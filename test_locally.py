#!/usr/bin/env python3
"""
ローカル環境でLambda関数をテストするためのスクリプト
"""
import os
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む（lambda_functionをインポートする前に行う必要がある）
load_dotenv()

from lambda_function import get_weather_forecast, format_pressure_message

def main():
    # APIキーが設定されているか確認
    api_key = os.environ.get('OPENWEATHER_API_KEY')
    if not api_key:
        print("エラー: OPENWEATHER_API_KEYが設定されていません。.envファイルを確認してください。")
        return
    
    print("OpenWeatherMap APIから天気データを取得中...")
    forecast_data = get_weather_forecast()
    
    if not forecast_data:
        print("天気データの取得に失敗しました。")
        return
    
    # 気圧メッセージをフォーマット
    message = format_pressure_message(forecast_data)
    
    # 結果を表示
    print("\n" + "="*50)
    print("フォーマットされたメッセージ:")
    print("="*50)
    print(message)

if __name__ == "__main__":
    main()
