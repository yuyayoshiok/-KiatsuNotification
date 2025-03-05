#!/bin/bash

# 変数の設定
DEPLOYMENT_DIR="deployment_package"
ZIP_FILE="lambda_deployment.zip"
FUNCTION_NAME="KiatsuNotification"

# 既存のデプロイメントディレクトリを削除して新規作成
echo "デプロイメントディレクトリを準備しています..."
rm -rf $DEPLOYMENT_DIR
mkdir -p $DEPLOYMENT_DIR

# 必要なファイルをコピー
echo "Lambda関数をコピーしています..."
cp lambda_function.py ./$DEPLOYMENT_DIR/
cp -r templates ./$DEPLOYMENT_DIR/

# 必要なパッケージをインストール
echo "必要なパッケージをインストールしています..."
cd $DEPLOYMENT_DIR
pip install -t . requests
pip install -t . python-dotenv
pip install -t . line-bot-sdk
pip install -t . boto3
pip install -t . pytz

# 不要なファイルを削除
echo "不要なファイルを削除しています..."
find . -type d -name "__pycache__" -exec rm -rf {} +
find . -type d -name "*.dist-info" -exec rm -rf {} +
find . -type d -name "*.egg-info" -exec rm -rf {} +

# ZIPファイルを作成
echo "ZIPファイルを作成しています..."
cd ..
rm -f $ZIP_FILE
cd $DEPLOYMENT_DIR
zip -r ../$ZIP_FILE .

# 作成したZIPファイルのサイズを表示
cd ..
echo "デプロイパッケージのサイズ:"
du -h $ZIP_FILE

echo "デプロイパッケージの作成が完了しました: $ZIP_FILE"

echo "デプロイが完了しました！"
