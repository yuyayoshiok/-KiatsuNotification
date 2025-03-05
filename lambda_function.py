#!/usr/bin/env python3
"""
気圧情報を取得してLINE通知を送信するLambda関数
"""
import os
import json
import logging
import requests
from dotenv import load_dotenv
import time
from datetime import datetime, timedelta
import warnings
from collections import Counter
import pytz
import boto3
import io

# LINE Bot SDKの非推奨警告を抑制
warnings.filterwarnings("ignore", category=DeprecationWarning, module="linebot")

# 直接実行時は.envから環境変数を読み込む
if __name__ == "__main__":
    try:
        print("環境変数を.envファイルから読み込みます...")
        load_dotenv()
        print("環境変数の読み込みが完了しました")
    except Exception as e:
        print(f"環境変数の読み込み中にエラーが発生しました: {str(e)}")


logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 環境変数を取得
OPENWEATHER_API_KEY = os.environ.get('OPENWEATHER_API_KEY')
CITY_ID = os.environ.get('CITY_ID', '1857550')  # 松江市のID
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_USER_ID = os.environ.get('LINE_USER_ID')
PRESSURE_THRESHOLD = float(os.environ.get('PRESSURE_THRESHOLD', '1010') or '1010')  # 低気圧の閾値
PRESSURE_CHANGE_THRESHOLD = float(os.environ.get('PRESSURE_CHANGE_THRESHOLD', '6') or '6')  # 気圧変化の閾値
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
USE_GROQ = os.environ.get('USE_GROQ', 'true')
S3_BUCKET_NAME = os.environ.get('S3_BUCKET_NAME', 'kiatsu-data')
S3_ENABLED = os.environ.get('S3_ENABLED', 'false').lower() == 'true'
PRESSURE_ALERT_THRESHOLD = float(os.environ.get('PRESSURE_ALERT_THRESHOLD', '8') or '8')  # 予測アラートの閾値
REGION_CUSTOMIZATION = os.environ.get('REGION_CUSTOMIZATION', 'false').lower() == 'true'
CUSTOM_CITY_IDS = os.environ.get('CUSTOM_CITY_IDS', '').split(',')  # カンマ区切りの都市ID

# 日本のタイムゾーン
JST = pytz.timezone('Asia/Tokyo')

# LINE Bot SDKのインポート
try:
    from linebot import LineBotApi
    from linebot.models import TextSendMessage, TextMessage
    LINE_SDK_AVAILABLE = True
except ImportError:
    LINE_SDK_AVAILABLE = False
    logger.warning("LINE Bot SDKがインストールされていません。LINE通知は無効になります。")

# LINE Bot APIの設定
if LINE_CHANNEL_ACCESS_TOKEN and LINE_SDK_AVAILABLE:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
else:
    logger.warning("LINE_CHANNEL_ACCESS_TOKENが設定されていません。LINE通知は無効になります。")
    line_bot_api = None

def get_day_name(date_obj):
    """
    日付から「月/日(曜日)」の形式の文字列を返す
    
    Args:
        date_obj (datetime.date): 日付オブジェクト
        
    Returns:
        str: 「月/日(曜日)」形式の文字列
    """
    weekdays = ['月', '火', '水', '木', '金', '土', '日']
    return f"{date_obj.month}/{date_obj.day}({weekdays[date_obj.weekday()]})"

def estimate_pressure_change(hourly_data):
    """
    24時間前の気圧を推定する
    
    Args:
        hourly_data (dict): 時間単位の天気予報データ
        
    Returns:
        float: 24時間前の気圧変化（hPa）
    """
    # 最初の6時間の気圧変化率を計算
    if len(hourly_data['list']) >= 3:
        first_pressure = hourly_data['list'][0]['main']['pressure']
        sixth_pressure = hourly_data['list'][2]['main']['pressure']
        pressure_change_rate = (sixth_pressure - first_pressure) / 6  # 6時間あたりの変化率
        
        # 24時間前の気圧を推定
        estimated_yesterday_pressure = first_pressure - (pressure_change_rate * 24)
        pressure_change_24h = first_pressure - estimated_yesterday_pressure
        return pressure_change_24h
    else:
        return None

def process_forecast_data(forecast_data):
    """
    5日間の天気予報データを日付ごとに処理する
    
    Args:
        forecast_data (dict): OpenWeatherMap APIからの天気予報データ
        
    Returns:
        dict: 日付ごとに処理されたデータ
    """
    daily_data = {}
    
    for item in forecast_data['list']:
        # 日時を解析
        dt = datetime.fromtimestamp(item['dt'], pytz.timezone('Asia/Tokyo'))
        date_str = dt.strftime('%Y-%m-%d')
        
        # 日付ごとのデータを初期化
        if date_str not in daily_data:
            daily_data[date_str] = {
                'pressures': [],
                'temps': [],
                'weather': [],
                'date': dt.date(),
                'day_name': get_day_name(dt.date())
            }
        
        # 気圧と温度を追加
        daily_data[date_str]['pressures'].append(item['main']['pressure'])
        daily_data[date_str]['temps'].append(item['main']['temp'])
        
        # 天気情報を追加
        daily_data[date_str]['weather'].append(item['weather'][0]['description'])
    
    # 各日の平均値と最大/最小値を計算
    for date_str, data in daily_data.items():
        data['avg_pressure'] = sum(data['pressures']) / len(data['pressures'])
        data['max_pressure'] = max(data['pressures'])
        data['min_pressure'] = min(data['pressures'])
        data['avg_temp'] = sum(data['temps']) / len(data['temps'])
        data['max_temp'] = max(data['temps'])
        data['min_temp'] = min(data['temps'])
        
        # 最も頻度の高い天気を取得
        weather_counter = Counter(data['weather'])
        data['common_weather'] = weather_counter.most_common(1)[0][0]
    
    return daily_data

def save_weather_data_to_s3(data, data_type='hourly'):
    """
    天気データをS3に保存する
    
    Args:
        data (dict): 保存する天気データ
        data_type (str): データタイプ（'hourly'または'daily'）
    
    Returns:
        bool: 保存が成功したかどうか
    """
    if not S3_ENABLED:
        logger.info("S3への保存は無効になっています")
        return False
    
    try:
        # S3クライアントの作成
        s3_client = boto3.client('s3')
        
        # 現在の日付を取得
        now = datetime.now(JST)
        date_str = now.strftime('%Y-%m-%d')
        
        # 保存するJSONデータを作成
        save_data = {
            'timestamp': now.isoformat(),
            'data': data
        }
        
        # ファイル名を設定
        file_name = f"{data_type}/{date_str}.json"
        
        # JSONデータをS3にアップロード
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=file_name,
            Body=json.dumps(save_data, ensure_ascii=False),
            ContentType='application/json'
        )
        
        logger.info(f"天気データをS3に保存しました: {file_name}")
        
        # 古いデータを削除（2日前以前のデータ）
        cleanup_old_weather_data(s3_client, data_type)
        
        return True
    except Exception as e:
        logger.error(f"S3への天気データの保存に失敗しました: {str(e)}")
        return False

def cleanup_old_weather_data(s3_client, data_type='hourly'):
    """
    2日前以前の古い天気データをS3から削除する
    
    Args:
        s3_client: boto3のS3クライアント
        data_type (str): データタイプ（'hourly'または'daily'）
    """
    try:
        # 現在の日付を取得
        now = datetime.now(JST)
        
        # 2日前の日付を計算
        two_days_ago = now - timedelta(days=2)
        cutoff_date = two_days_ago.date()
        
        # S3バケット内のオブジェクトリストを取得
        response = s3_client.list_objects_v2(
            Bucket=S3_BUCKET_NAME,
            Prefix=f"{data_type}/"
        )
        
        # 削除対象のオブジェクトを特定
        if 'Contents' in response:
            for obj in response['Contents']:
                key = obj['Key']
                
                # ファイル名から日付を抽出（例: hourly/2025-03-02.json）
                try:
                    file_date_str = key.split('/')[1].split('.')[0]
                    file_date = datetime.strptime(file_date_str, '%Y-%m-%d').date()
                    
                    # 2日前より古いファイルを削除
                    if file_date < cutoff_date:
                        s3_client.delete_object(
                            Bucket=S3_BUCKET_NAME,
                            Key=key
                        )
                        logger.info(f"古い天気データを削除しました: {key}")
                except Exception as e:
                    logger.warning(f"ファイル日付の解析に失敗しました: {key}, エラー: {str(e)}")
    except Exception as e:
        logger.error(f"古い天気データの削除に失敗しました: {str(e)}")

def get_previous_day_weather_data(data_type='hourly'):
    """
    前日の天気データをS3から取得する
    
    Args:
        data_type (str): データタイプ（'hourly'または'daily'）
    
    Returns:
        dict: 前日の天気データ、取得できない場合はNone
    """
    if not S3_ENABLED:
        logger.info("S3からのデータ取得は無効になっています")
        return None
    
    try:
        # S3クライアントの作成
        s3_client = boto3.client('s3')
        
        # 前日の日付を計算
        yesterday = datetime.now(JST) - timedelta(days=1)
        yesterday_str = yesterday.strftime('%Y-%m-%d')
        
        # ファイル名を設定
        file_name = f"{data_type}/{yesterday_str}.json"
        
        # S3からデータを取得
        response = s3_client.get_object(
            Bucket=S3_BUCKET_NAME,
            Key=file_name
        )
        
        # JSONデータを読み込む
        data = json.loads(response['Body'].read().decode('utf-8'))
        
        logger.info(f"前日の天気データをS3から取得しました: {file_name}")
        return data['data']
    except Exception as e:
        logger.warning(f"前日の天気データの取得に失敗しました: {str(e)}")
        return None

def get_weather_forecast():
    """
    OpenWeatherMap APIから5日間の天気予報データを取得する
    
    Returns:
        dict: 天気予報データ、エラーの場合はNone
    """
    if not OPENWEATHER_API_KEY:
        logger.error("OpenWeatherMap APIキーが設定されていません")
        return None
    
    url = f"https://api.openweathermap.org/data/2.5/forecast?id={CITY_ID}&appid={OPENWEATHER_API_KEY}&units=metric&lang=ja"
    
    try:
        response = requests.get(url)
        response.raise_for_status()  # エラーレスポンスの場合は例外を発生
        
        data = response.json()
        logger.info("5日間の天気予報データを取得しました")
        
        # S3にデータを保存
        if S3_ENABLED:
            save_weather_data_to_s3(data, 'daily')
        
        return data
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            logger.error("OpenWeatherMap APIキーが無効です")
        else:
            logger.error(f"天気予報データの取得中にHTTPエラーが発生しました: {str(e)}")
        
        # ダミーデータを生成
        return generate_dummy_forecast_data()
    except Exception as e:
        logger.error(f"天気予報データの取得中にエラーが発生しました: {str(e)}")
        
        # ダミーデータを生成
        return generate_dummy_forecast_data()

def get_hourly_weather():
    """
    OpenWeatherMap APIから時間単位の天気予報データを取得する
    
    Args:
        None
    
    Returns:
        dict: 時間単位の天気予報データ、エラーの場合はNone
    """
    if not OPENWEATHER_API_KEY:
        logger.error("OpenWeatherMap APIキーが設定されていません")
        return None
    
    url = f"https://api.openweathermap.org/data/2.5/forecast?id={CITY_ID}&appid={OPENWEATHER_API_KEY}&units=metric&lang=ja"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        
        data = response.json()
        logger.info("時間単位の天気予報データを取得しました")
        
        # S3にデータを保存
        if S3_ENABLED:
            save_weather_data_to_s3(data, 'hourly')
        
        return data
    except Exception as e:
        logger.error(f"時間単位の天気予報データの取得に失敗しました: {str(e)}")
        
        # ダミーデータを生成
        return generate_dummy_hourly_data()

def get_pressure_health_advice(pressure_data, weather_condition=None):
    """
    気圧データと天気状況に基づいて健康アドバイスを生成する
    
    Args:
        pressure_data (dict): 気圧データ
        weather_condition (str, optional): 天気状況
    
    Returns:
        str: 健康アドバイス
    """
    # Groq APIを試す
    if USE_GROQ and USE_GROQ.lower() == 'true' and GROQ_API_KEY:
        try:
            # requestsを使用してGroq APIを直接呼び出す
            import requests
            import json
            
            # プロンプトの作成
            current_pressure = pressure_data.get('current_pressure', 'N/A')
            pressure_change = pressure_data.get('pressure_change', 'N/A')
            
            # 気圧変化の説明
            change_description = ""
            if pressure_change != 'N/A':
                if pressure_change > 0:
                    change_description = f"気圧は24時間で{pressure_change}hPa上昇しています。"
                elif pressure_change < 0:
                    change_description = f"気圧は24時間で{abs(pressure_change)}hPa下降しています。"
                else:
                    change_description = "気圧は24時間で変化していません。"
            
            # 天気状況の追加
            weather_info = ""
            if weather_condition:
                weather_info = f"現在の天気: {weather_condition}"
            
            prompt = f"""
            あなたは気象と健康の専門家です。以下の気象情報に基づいて、友人に対するように親しみやすく、会話的な健康アドバイスを提供してください。

            🌞 現在の気圧は{current_pressure}hPaです。{change_description}
            {weather_info}
            
            以下の点を考慮した会話的なアドバイスを提供してください:
            1. 気圧と天気が人の体調に与える影響について簡潔に説明
            2. この気象条件下での具体的な対策や予防法
            3. 食事や運動に関する実用的なアドバイス
            4. 気分を良くするための小さなヒント
            
            回答は以下の形式で提供してください:
            - タイトルは親しみやすく、今日の気象条件を反映したものにする
            - 最初に短い挨拶と現在の気象状況の詳細な説明
            - 2〜3個の具体的なアドバイスを箇条書きで
            - 各アドバイスの前に適切な絵文字を付ける
            - 最後に前向きな一言で締めくくる
            
            全体の長さは300文字以内に収めてください。親しみやすく、会話的な口調で書いてください。
            """
            
            # Groq APIを直接呼び出し
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {GROQ_API_KEY}"
            }
            
            payload = {
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": 500
            }
            
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                data=json.dumps(payload)
            )
            
            if response.status_code == 200:
                response_data = response.json()
                advice = response_data["choices"][0]["message"]["content"].strip()
                
                logger.info("Groq APIを使用して健康アドバイスを生成しました")
                return f"\n【健康アドバイス】\n{advice}"
            else:
                logger.error(f"Groq APIの呼び出しに失敗しました: {response.status_code} {response.text}")
                # Groqが失敗した場合、デフォルトのアドバイスを返す
                return get_default_health_advice(weather_condition)
                
        except Exception as e:
            logger.error(f"Groq APIの呼び出しに失敗しました: {str(e)}")
            # Groqが失敗した場合、デフォルトのアドバイスを返す
            return get_default_health_advice(weather_condition)
    
    # Groq APIが無効な場合はデフォルトのアドバイスを返す
    logger.info("Groq APIは無効または利用できません。デフォルトのアドバイスを使用します。")
    return get_default_health_advice(weather_condition)

def get_default_health_advice(weather_condition=None):
    """
    デフォルトの健康アドバイスを返す
    
    Args:
        weather_condition (str, optional): 天気状況
    
    Returns:
        str: デフォルトの健康アドバイス
    """
    logger.info("デフォルトの健康アドバイスを使用します")
    
    # 天気に基づいたアドバイスを生成
    if weather_condition:
        weather_condition = weather_condition.lower()
        
        if '雨' in weather_condition:
            return """
【雨天時の健康アドバイス】
☔ 湿度管理を心がける
🍵 温かい飲み物を摂る
🧘‍♀️ リラックスする時間を作る
💪 室内でストレッチを行う
"""
        elif '雪' in weather_condition:
            return """
【雪の日の健康アドバイス】
❄️ 転倒に注意する
🧣 防寒対策をしっかりと
🍲 温かい食事を心がける
🚶‍♂️ 無理な外出は控える
"""
        elif '曇' in weather_condition:
            return """
【曇り空の健康アドバイス】
😊 ポジティブな気持ちを保つ
🚶‍♀️ 軽い運動を取り入れる
🥗 ビタミンDを意識した食事
💡 明るい照明で気分転換
"""
        elif '晴' in weather_condition:
            return """
【晴れの日の健康アドバイス】
☀️ 適切な日焼け対策を
💧 こまめな水分補給を
🏃‍♂️ 外での適度な運動を
🥗 新鮮な野菜・果物を摂る
"""
    
    # 天気情報がない場合の一般的なアドバイス
    return """
【一般的な健康アドバイス】
💧 水分をこまめに摂取する
🚶‍♀️ 適度な運動を心がける
😴 十分な睡眠をとる
🍎 バランスの良い食事を
"""

def format_pressure_message(forecast_data):
    """
    気圧データをフォーマットしてメッセージを作成する
    """
    if not forecast_data or 'list' not in forecast_data:
        return "天気予報データの取得に失敗しました。"
    
    # 日付ごとに気圧データをグループ化
    daily_data = process_forecast_data(forecast_data)
    
    # メッセージを作成
    message_parts = []
    message_parts.append("【松江市の気圧情報】")
    
    # 気圧変化に関する会話的なコメントを追加
    message_parts.append("\n【気圧予報のポイント】")
    if len(daily_data) > 1:
        today_pressure = daily_data[list(daily_data.keys())[0]]['avg_pressure']
        tomorrow_pressure = daily_data[list(daily_data.keys())[1]]['avg_pressure']
        change = tomorrow_pressure - today_pressure
        
        if change > PRESSURE_CHANGE_THRESHOLD:
            message_parts.append(f"明日は気圧が{abs(change):.1f}hPa上昇する予報です。頭痛や関節痛が出やすくなるかもしれません。水分をしっかり取って、無理をしないようにしましょう。")
        elif change > 0:
            message_parts.append(f"明日は気圧が{abs(change):.1f}hPa上昇する予報です。体調の変化に注意して、十分な休息を取るようにしましょう。")
        elif change < -PRESSURE_CHANGE_THRESHOLD:
            message_parts.append(f"明日は気圧が{abs(change):.1f}hPa下降する予報です。自律神経に影響が出やすいので、ゆっくり休息を取り、温かい飲み物を摂るといいでしょう。")
        elif change < 0:
            message_parts.append(f"明日は気圧が{abs(change):.1f}hPa下降する予報です。疲れやすく感じるかもしれないので、無理せず過ごしましょう。")
        else:
            message_parts.append("明日も気圧は安定しています。快適に過ごせる一日になりそうです。")
        
        # 低気圧の日がある場合
        low_pressure_days = []
        for date_str in daily_data:
            day_data = daily_data[date_str]
            if day_data['avg_pressure'] < PRESSURE_THRESHOLD:
                low_pressure_days.append(day_data['day_name'])
        
        if low_pressure_days:
            message_parts.append(f"{', '.join(low_pressure_days)}は低気圧になる予報です。体調管理に気をつけましょう。")
    
    # 現在の気圧
    current_pressure = daily_data[list(daily_data.keys())[0]]['avg_pressure']
    message_parts.append(f"現在の気圧: {current_pressure:.0f}hPa")
    
    # 低気圧の日をチェック
    low_pressure_days = []
    for date_str in daily_data:
        day_data = daily_data[date_str]
        if day_data['avg_pressure'] < PRESSURE_THRESHOLD:
            low_pressure_days.append(day_data['day_name'])
    
    if low_pressure_days:
        message_parts.append(f"低気圧の日: {', '.join(low_pressure_days)}")
    
    # 気圧変化をチェック
    pressure_changes = []
    for i in range(1, len(daily_data)):
        prev_date = list(daily_data.keys())[i-1]
        curr_date = list(daily_data.keys())[i]
        prev_pressure = daily_data[prev_date]['avg_pressure']
        curr_pressure = daily_data[curr_date]['avg_pressure']
        change = curr_pressure - prev_pressure
        
        if abs(change) >= PRESSURE_CHANGE_THRESHOLD:
            direction = "上昇" if change > 0 else "下降"
            pressure_changes.append(f"{daily_data[curr_date]['day_name']}に{abs(change):.1f}hPa{direction}")
    
    if pressure_changes:
        message_parts.append(f"気圧変化: {', '.join(pressure_changes)}")
    
    # 5日間の気圧予報
    message_parts.append("\n【5日間気圧予報】")
    for date_str in daily_data:
        day_data = daily_data[date_str]
        date_obj = day_data['date']
        day_name = day_data['day_name']
        avg_pressure = day_data['avg_pressure']
        common_weather = day_data['common_weather']
        
        message_parts.append(f"{day_name}: {avg_pressure:.0f}hPa ({common_weather})")
    
    # 前日との気圧差を計算
    pressure_change = None
    if len(daily_data) > 1:
        current_pressure = daily_data[list(daily_data.keys())[0]]['avg_pressure']
        tomorrow_pressure = daily_data[list(daily_data.keys())[1]]['avg_pressure']
        pressure_change = tomorrow_pressure - current_pressure
    
    # 最も頻度の高い天気
    common_weather = daily_data[list(daily_data.keys())[0]]['common_weather']
    
    # 健康アドバイスを取得
    use_groq = USE_GROQ.lower() == 'true' if USE_GROQ else False
    health_advice = get_pressure_health_advice({'current_pressure': current_pressure, 'pressure_change': pressure_change}, common_weather)
    message_parts.append(health_advice)
    
    return "\n".join(message_parts)

def detect_pressure_alert(hourly_data):
    """
    今後24時間以内に急激な気圧変化が予測されるかを検出する
    
    Args:
        hourly_data (dict): 時間単位の天気予報データ
        
    Returns:
        tuple: (アラートが必要かどうか, アラートメッセージ, 最大気圧変化, 変化時間)
    """
    if not hourly_data or 'list' not in hourly_data or len(hourly_data['list']) < 8:
        return False, "", 0, ""
    
    # 今後24時間の時間単位データを取得
    forecast_hours = hourly_data['list'][:8]  # 3時間ごとのデータなので8ポイントで24時間
    
    max_pressure_change = 0
    max_change_time = None
    previous_pressure = forecast_hours[0]['main']['pressure']
    
    # 各時間の気圧変化を計算
    for i in range(1, len(forecast_hours)):
        current_pressure = forecast_hours[i]['main']['pressure']
        pressure_change = abs(current_pressure - previous_pressure)
        
        if pressure_change > max_pressure_change:
            max_pressure_change = pressure_change
            max_change_time = datetime.fromtimestamp(forecast_hours[i]['dt'], JST)
        
        previous_pressure = current_pressure
    
    # 3時間あたりの変化率がPRESSURE_ALERT_THRESHOLDを超える場合にアラート
    if max_pressure_change >= PRESSURE_ALERT_THRESHOLD:
        alert_message = f"⚠️ 気圧変化アラート ⚠️\n{max_change_time.strftime('%m/%d %H:%M')}頃に{max_pressure_change:.1f}hPaの急激な気圧変化が予測されています。体調の変化に注意してください。"
        return True, alert_message, max_pressure_change, max_change_time.strftime('%m/%d %H:%M')
    
    return False, "", max_pressure_change, ""

def format_hourly_pressure_message(hourly_data):
    """
    時間単位の気圧データをフォーマットしてメッセージを作成する
    
    Args:
        hourly_data (dict): 時間単位の天気予報データ
        
    Returns:
        str: フォーマットされたメッセージ
    """
    if not hourly_data or 'list' not in hourly_data:
        return "時間単位の気圧データを取得できませんでした。"
    
    # 24時間気圧予報のメッセージを作成
    message = "【24時間気圧予報】\n"
    
    # 最初の気圧を取得
    first_item = hourly_data['list'][0]
    first_pressure = first_item['main']['pressure']
    first_dt = datetime.fromtimestamp(first_item['dt'], JST)
    message += f"{first_dt.strftime('%m/%d %H:%M')} {first_pressure}hPa\n"
    
    # 最後の気圧を取得（24時間後 = 8ポイント目、3時間ごとのデータ）
    if len(hourly_data['list']) >= 8:
        last_item = hourly_data['list'][7]
        last_pressure = last_item['main']['pressure']
        last_dt = datetime.fromtimestamp(last_item['dt'], JST)
        message += f"{last_dt.strftime('%m/%d %H:%M')} {last_pressure}hPa\n"
    
    # 24時間の気圧変化を計算
    if len(hourly_data['list']) >= 8:
        pressure_change_24h = hourly_data['list'][7]['main']['pressure'] - first_pressure
        change_symbol = "→"
        if pressure_change_24h > 0:
            change_symbol = "↑"
        elif pressure_change_24h < 0:
            change_symbol = "↓"
        
        message += f"\n24時間変化: {change_symbol} {abs(pressure_change_24h):.1f}hPa\n"
    
    # 予測アラートの検出
    has_alert, alert_message, max_change, change_time = detect_pressure_alert(hourly_data)
    if has_alert:
        message += f"\n{alert_message}\n"
    
    # 気圧変化に基づく健康アドバイスを取得
    pressure_data = {
        'current': first_pressure,
        'change_24h': pressure_change_24h if 'pressure_change_24h' in locals() else 0
    }
    
    # 天気状況を取得
    weather_condition = None
    if 'weather' in first_item and len(first_item['weather']) > 0:
        weather_condition = first_item['weather'][0]['description']
    
    # 健康アドバイスを取得
    health_advice = get_pressure_health_advice(pressure_data, weather_condition)
    
    # メッセージに健康アドバイスを追加
    message += f"\n【健康アドバイス】\n{health_advice}"
    
    return message

def generate_dummy_forecast_data():
    """
    ダミーの5日間天気予報データを生成する
    
    Returns:
        dict: ダミーの天気予報データ
    """
    logger.info("ダミーの5日間天気予報データを生成します")
    
    now = datetime.now(JST)
    forecast_list = []
    
    # 5日間、1日8ポイント（3時間ごと）のデータを生成
    for day in range(5):
        for hour in range(0, 24, 3):
            # 日時を計算
            dt = now + timedelta(days=day, hours=hour)
            timestamp = int(dt.timestamp())
            
            # 気圧は1000〜1020の間でランダムに変動（日によって傾向を持たせる）
            base_pressure = 1010 + day * 2  # 日ごとに少し上昇
            pressure = base_pressure + (hour - 12) * 0.5  # 時間帯による変動
            
            # 天気の種類
            weather_types = ["晴れ", "曇り", "小雨", "適度な雨"]
            weather_type = weather_types[day % len(weather_types)]
            
            # データポイントを作成
            data_point = {
                "dt": timestamp,
                "main": {
                    "temp": 20 + day + (hour - 12) * 0.5,  # 気温
                    "feels_like": 18 + day + (hour - 12) * 0.5,
                    "temp_min": 18 + day,
                    "temp_max": 22 + day,
                    "pressure": pressure,
                    "humidity": 70 - day * 5
                },
                "weather": [
                    {
                        "id": 800 + day * 100,
                        "main": weather_type,
                        "description": weather_type,
                        "icon": "01d"
                    }
                ],
                "clouds": {
                    "all": day * 20
                },
                "wind": {
                    "speed": 2 + day * 0.5,
                    "deg": 180 + day * 10
                },
                "visibility": 10000 - day * 1000,
                "pop": day * 0.2,
                "sys": {
                    "pod": "d" if 6 <= hour < 18 else "n"
                },
                "dt_txt": dt.strftime("%Y-%m-%d %H:%M:%S")
            }
            
            forecast_list.append(data_point)
    
    # 完全なレスポンス形式を作成
    dummy_data = {
        "cod": "200",
        "message": 0,
        "cnt": len(forecast_list),
        "list": forecast_list,
        "city": {
            "id": int(CITY_ID),
            "name": "東京都港区",
            "coord": {
                "lat": 35.6581,
                "lon": 139.7414
            },
            "country": "JP",
            "timezone": 32400,
            "sunrise": int((now.replace(hour=6, minute=0, second=0) - timedelta(hours=9)).timestamp()),
            "sunset": int((now.replace(hour=18, minute=0, second=0) - timedelta(hours=9)).timestamp())
        }
    }
    
    return dummy_data

def generate_dummy_hourly_data():
    """
    ダミーの時間単位天気予報データを生成する
    
    Returns:
        dict: ダミーの時間単位天気予報データ
    """
    # 5日間予報と同じフォーマットを使用
    return generate_dummy_forecast_data()

def send_line_notification(message):
    """
    LINEにメッセージを送信する
    
    Args:
        message (str): 送信するメッセージ
    """
    if not LINE_SDK_AVAILABLE or not line_bot_api:
        logger.warning("LINE Bot SDKが利用できないため、通知を送信できません。")
        return

    try:
        # メッセージの送信
        line_bot_api.push_message(LINE_USER_ID, TextSendMessage(text=message))
        logger.info("LINE通知を送信しました")
    except Exception as e:
        logger.error(f"LINE通知の送信に失敗しました: {str(e)}")
        raise

def get_custom_region_forecast(city_id):
    """
    カスタム地域の気圧情報を取得してフォーマットする
    
    Args:
        city_id (str): OpenWeatherMap APIの都市ID
        
    Returns:
        str: フォーマットされたメッセージ、エラーの場合はNone
    """
    try:
        # 都市IDを使用して天気予報を取得
        url = f"https://api.openweathermap.org/data/2.5/forecast?id={city_id}&units=metric&lang=ja&appid={OPENWEATHER_API_KEY}"
        response = requests.get(url)
        
        if response.status_code != 200:
            logger.error(f"カスタム地域の天気データ取得エラー: {response.status_code}")
            return None
        
        forecast_data = response.json()
        
        # 都市名を取得
        city_name = forecast_data['city']['name']
        
        # 最初のデータポイントの気圧と天気を取得
        first_item = forecast_data['list'][0]
        current_pressure = first_item['main']['pressure']
        current_weather = first_item['weather'][0]['description']
        current_temp = first_item['main']['temp']
        
        # 24時間後のデータポイントを取得（3時間ごとのデータなので8ポイント目）
        future_pressure = None
        future_weather = None
        future_temp = None
        if len(forecast_data['list']) >= 8:
            future_item = forecast_data['list'][7]
            future_pressure = future_item['main']['pressure']
            future_weather = future_item['weather'][0]['description']
            future_temp = future_item['main']['temp']
        
        # メッセージを作成
        message = f"【{city_name}の気圧情報】\n"
        message += f"現在の気圧: {current_pressure}hPa（{current_weather}、{current_temp:.1f}℃）\n"
        
        if future_pressure:
            # 気圧変化を計算
            pressure_change = future_pressure - current_pressure
            
            # 気圧変化の矢印
            arrow = "→"
            if pressure_change > 1:
                arrow = "↑"
            elif pressure_change < -1:
                arrow = "↓"
            
            message += f"24時間後の予測: {future_pressure}hPa（{future_weather}、{future_temp:.1f}℃）\n"
            message += f"変化: {arrow} {pressure_change}hPa\n"
            
            # 急激な気圧変化の警告
            if abs(pressure_change) >= PRESSURE_CHANGE_THRESHOLD:
                message += f"\n⚠️ 24時間以内に{abs(pressure_change):.1f}hPaの気圧変化が予測されています。体調の変化に注意してください。\n"
        
        return message
        
    except Exception as e:
        logger.error(f"カスタム地域の天気データ処理エラー: {str(e)}")
        return None

def lambda_handler(event, context):
    """
    AWS Lambdaのハンドラー関数
    """
    try:
        logger.info("気圧通知処理を開始します")
        
        # 天気予報データを取得
        forecast_data = get_weather_forecast()
        hourly_data = get_hourly_weather()
        
        if not forecast_data or not hourly_data:
            logger.error("天気データの取得に失敗しました")
            return {
                'statusCode': 500,
                'body': json.dumps('天気データの取得に失敗しました')
            }
        
        # S3にデータを保存
        if S3_ENABLED:
            save_weather_data_to_s3(forecast_data, 'daily')
            save_weather_data_to_s3(hourly_data, 'hourly')
        
        # 気圧メッセージをフォーマット
        message = format_pressure_message(forecast_data)
        hourly_message = format_hourly_pressure_message(hourly_data)
        
        # LINE通知を送信
        send_line_notification(message)
        send_line_notification(hourly_message)
        
        # 地域カスタマイズが有効な場合、追加の地域情報を取得して通知
        if REGION_CUSTOMIZATION and CUSTOM_CITY_IDS:
            for city_id in CUSTOM_CITY_IDS:
                if city_id.strip():  # 空でない場合のみ処理
                    custom_message = get_custom_region_forecast(city_id.strip())
                    if custom_message:
                        send_line_notification(custom_message)
        
        logger.info("気圧通知処理が完了しました")
        
        return {
            'statusCode': 200,
            'body': json.dumps('気圧通知処理が完了しました')
        }
        
    except Exception as e:
        logger.error(f"エラーが発生しました: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps(f'エラーが発生しました: {str(e)}')
        }

# スクリプトとして実行された場合
if __name__ == "__main__":
    # .envファイルから環境変数を読み込む
    try:
        print("環境変数を.envファイルから読み込みます...")
        from dotenv import load_dotenv
        load_dotenv()
        print("環境変数の読み込みが完了しました")
    except Exception as e:
        print(f"環境変数の読み込み中にエラーが発生しました: {str(e)}")
    
    # Lambda関数を実行
    lambda_handler(None, None)

# LINE Webhookからのイベント処理
def lambda_handler(event, context):
    # 既存の定期実行処理（EventBridgeからのトリガー）
    if event.get('source') == 'aws.events':
        return process_scheduled_event()
    
    # LINE Webhookからのリクエスト処理
    try:
        print(f"Received event: {json.dumps(event)}")
        
        # リクエスト本文の取得
        body = None
        if 'body' in event:
            if isinstance(event['body'], str):
                body = json.loads(event['body'])
            else:
                body = event['body']
        
        if not body or 'events' not in body:
            return {
                'statusCode': 200,
                'headers': {
                    'Content-Type': 'application/json'
                },
                'body': json.dumps({'message': 'No events in request body'})
            }
        
        events = body.get('events', [])
        
        for line_event in events:
            if line_event['type'] == 'message' and line_event['message']['type'] == 'text':
                user_id = line_event['source']['userId']
                reply_token = line_event['replyToken']
                message_text = line_event['message']['text']
                
                # 市名の処理（「〇〇市」のパターンを検出）
                if '市' in message_text:
                    # ユーザーの設定を保存
                    save_user_city_preference(user_id, message_text)
                    process_city_request(message_text, reply_token)
                else:
                    # ユーザーの設定を取得して処理
                    city_preference = get_user_city_preference(user_id)
                    if city_preference:
                        process_city_request(city_preference, reply_token, f"あなたの設定: {city_preference}\n\n")
                    else:
                        # その他のメッセージ処理
                        line_bot_api.reply_message(
                            reply_token,
                            TextSendMessage(text="気圧情報を知りたい地域名を「松江市」「出雲市」のように入力してください。")
                        )
        
        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json'
            },
            'body': json.dumps({'message': 'Success'})
        }
    except Exception as e:
        print(f"Error processing LINE webhook: {str(e)}")
        return {
            'statusCode': 200,  # LINEは200以外のステータスコードを受け付けない
            'headers': {
                'Content-Type': 'application/json'
            },
            'body': json.dumps({'message': f"Error: {str(e)}"})
        }

# ユーザーの市設定を保存する関数
def save_user_city_preference(user_id, city_name):
    try:
        if os.environ.get('S3_ENABLED', 'false').lower() == 'true':
            s3 = boto3.resource('s3')
            bucket_name = os.environ.get('S3_BUCKET_NAME', 'kiatsu-data')
            s3.Object(bucket_name, f'user_preferences/{user_id}.json').put(
                Body=json.dumps({'city': city_name})
            )
            print(f"Saved user preference for {user_id}: {city_name}")
        else:
            print("S3 is not enabled, user preference not saved")
    except Exception as e:
        print(f"Error saving user preference: {str(e)}")

# ユーザーの市設定を取得する関数
def get_user_city_preference(user_id):
    try:
        if os.environ.get('S3_ENABLED', 'false').lower() == 'true':
            s3 = boto3.resource('s3')
            bucket_name = os.environ.get('S3_BUCKET_NAME', 'kiatsu-data')
            try:
                obj = s3.Object(bucket_name, f'user_preferences/{user_id}.json').get()
                data = json.loads(obj['Body'].read().decode('utf-8'))
                return data.get('city')
            except s3.meta.client.exceptions.NoSuchKey:
                print(f"No preference found for user {user_id}")
                return None
        else:
            print("S3 is not enabled, cannot retrieve user preference")
            return None
    except Exception as e:
        print(f"Error getting user preference: {str(e)}")
        return None

# 市名リクエストの処理
def process_city_request(city_name, reply_token, prefix=""):
    try:
        # 市名から都市IDを検索
        city_id = get_city_id_by_name(city_name)
        
        if city_id:
            # 都市IDから気圧情報を取得
            weather_data = get_weather_data_by_city_id(city_id)
            
            # 気圧情報を整形
            message = format_city_pressure_message(city_name, weather_data)
            
            # プレフィックスを追加（ユーザー設定の表示など）
            if prefix:
                message = prefix + message
            
            # LINE Botから返信
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text=message)
            )
        else:
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text=f"{city_name}の情報が見つかりませんでした。島根県内の市名（例：松江市、出雲市）を入力してください。")
            )
    except Exception as e:
        print(f"Error processing city request: {str(e)}")
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text="エラーが発生しました。しばらく経ってからもう一度お試しください。")
        )

# 既存の定期実行処理
def process_scheduled_event():
    try:
        # 既存の処理をここに移動
        forecast_data = get_weather_forecast()
        
        # S3にデータを保存（設定されている場合）
        if os.environ.get('S3_ENABLED', 'false').lower() == 'true':
            save_forecast_to_s3(forecast_data)
        
        # 気圧メッセージを作成
        message = format_pressure_message(forecast_data)
        
        # Groq APIによる健康アドバイスを追加（設定されている場合）
        if os.environ.get('USE_GROQ', 'false').lower() == 'true':
            advice = get_health_advice_from_groq(forecast_data)
            if advice:
                message += f"\n\n{advice}"
        
        # LINE通知を送信
        send_line_notification(message)
        
        # 予測アラート機能
        detect_pressure_alert(forecast_data)
        
        # 地域カスタマイズ機能
        if os.environ.get('REGION_CUSTOMIZATION', 'false').lower() == 'true':
            custom_city_ids = os.environ.get('CUSTOM_CITY_IDS', '').split(',')
            for city_id in custom_city_ids:
                if city_id.strip():
                    custom_forecast = get_custom_region_forecast(city_id.strip())
                    if custom_forecast:
                        custom_message = format_custom_region_message(custom_forecast)
                        send_line_notification(custom_message)
        
        return {
            'statusCode': 200,
            'body': json.dumps('Weather notification sent successfully!')
        }
    except Exception as e:
        print(f"Error in process_scheduled_event: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps(f"Error: {str(e)}")
        }

# 市名から都市IDを検索する関数
def get_city_id_by_name(city_name):
    # 島根県内の市→都市ID変換マップ
    shimane_city_map = {
        "松江市": "1857550",  # 松江市
        "出雲市": "1861084",  # 出雲市
        "浜田市": "1863289",  # 浜田市
        "益田市": "1857553",  # 益田市
        "大田市": "1853140",  # 大田市
        "安来市": "1849796",  # 安来市
        "江津市": "1860563",  # 江津市
        "雲南市": "1848689"   # 雲南市
    }
    
    # 市名から都市IDを検索
    for key, value in shimane_city_map.items():
        if key in city_name:
            return value
    
    return None

# 都市IDから気圧情報を取得する関数
def get_weather_data_by_city_id(city_id):
    api_key = os.environ['OPENWEATHER_API_KEY']
    url = f"https://api.openweathermap.org/data/2.5/forecast?id={city_id}&appid={api_key}&units=metric"
    
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"OpenWeatherMap API error: {response.status_code}")

# 気圧情報を整形するメッセージ
def format_city_pressure_message(city_name, weather_data):
    city = weather_data['city']['name']
    current_data = weather_data['list'][0]
    current_pressure = current_data['main']['pressure']
    current_time = datetime.fromtimestamp(current_data['dt'])
    
    # 24時間後のデータを取得（3時間ごとのデータなので8番目のデータ）
    future_data = weather_data['list'][min(8, len(weather_data['list'])-1)]
    future_pressure = future_data['main']['pressure']
    future_time = datetime.fromtimestamp(future_data['dt'])
    
    pressure_change = future_pressure - current_pressure
    
    # 気圧変化の矢印
    arrow = "→"
    if pressure_change > 1:
        arrow = "↑"
    elif pressure_change < -1:
        arrow = "↓"
    
    message = f"{city_name}の気圧情報:\n"
    message += f"現在の気圧: {current_pressure}hPa ({current_time.strftime('%m/%d %H:%M')})\n"
    message += f"24時間後の予測: {future_pressure}hPa ({future_time.strftime('%m/%d %H:%M')})\n"
    message += f"変化: {arrow} {pressure_change}hPa\n"
    
    # 低気圧や急激な変化がある場合の警告
    pressure_threshold = int(os.environ.get('PRESSURE_THRESHOLD', 1010))
    pressure_change_threshold = int(os.environ.get('PRESSURE_CHANGE_THRESHOLD', 6))
    
    if current_pressure < pressure_threshold:
        message += "\n⚠️ 現在低気圧です。体調の変化に注意してください。"
    
    if abs(pressure_change) >= pressure_change_threshold:
        message += f"\n⚠️ 24時間以内に{abs(pressure_change)}hPaの気圧変化が予測されています。体調の変化に注意してください。"
    
    return message
