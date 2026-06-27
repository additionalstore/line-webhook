import os
import threading
import smtplib
from email.mime.text import MIMEText
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])

SYSTEM_PROMPT = """
あなたは「有限会社サラ（Additional Store）」のカスタマーサポート担当です。
以下の会社情報をもとに、丁寧で簡潔な返信メッセージの案を作成してください。

【会社情報】
- 会社名: 有限会社サラ / Additional Store
- 所在地: 東京都高円寺
- 事業内容: 国内自社ファクトリーによる刺繍加工・アパレルOEM・グッズODM・ノベルティ制作
- 特徴: 3D立体刺繍・高密度加工・小ロット対応・デザイン提案から本生産まで一気通貫
- 取引実績: ベイクルーズ、ユナイテッドアローズ、カンタベリーなど有名ブランド

【連絡先】
- ショップ: 03-5913-7719
- ファクトリー直通: 03-5364-9934
- LINE: lin.ee/1LrS61G
- Instagram: @additional_store

【返信ルール】
- 丁寧な日本語で書く
- 具体的な価格・納期は「改めてお見積りが必要」と伝え、電話・LINEへ誘導する
- LINEなので短めに要点を絞って書く（200文字以内が目安）
- 署名は不要
"""


def generate_reply(user_message):
    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    response = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{'role': 'user', 'content': user_message}]
    )
    return response.content[0].text


def send_gmail_notification(user_message, reply_suggestion):
    gmail_user = os.environ.get('GMAIL_USER', 'miyata.4078@gmail.com')
    gmail_password = os.environ.get('GMAIL_APP_PASSWORD', '')
    notify_to = os.environ.get('NOTIFY_TO', 'miyata.4078@gmail.com')

    subject = f'【LINE返信案】{user_message[:20]}...'
    body = f"""LINEにメッセージが届きました。

【受信メッセージ】
{user_message}

【返信案（Claude生成）】
{reply_suggestion}

---
LINE Official Account Manager で確認・送信してください:
https://manager.line.biz/
"""

    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = gmail_user
    msg['To'] = notify_to

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(gmail_user, gmail_password)
            server.send_message(msg)
        print(f'Gmail通知送信完了')
    except Exception as e:
        print(f'Gmail通知エラー: {e}')


def process_message_background(user_message, user_id):
    """バックグラウンドでClaude生成とGmail送信を実行（タイムアウト対策）"""
    print(f'バックグラウンド処理開始: {user_message[:30]}')
    try:
        reply_suggestion = generate_reply(user_message)
        print('Claude返信案生成完了')
    except Exception as e:
        print(f'Claude APIエラー: {e}')
        reply_suggestion = '（返信案の生成に失敗しました）'

    send_gmail_notification(user_message, reply_suggestion)
    print('バックグラウンド処理完了')


@app.route('/callback', methods=['POST'])
def callback():
    print('=== LINEからWebhook受信 ===')
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    print(f'ボディ: {body[:200]}')

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print('署名エラー')
        abort(400)

    return 'OK'


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text
    user_id = event.source.user_id
    print(f'受信: {user_message}')

    # バックグラウンドスレッドで処理し、すぐに200 OKを返す
    thread = threading.Thread(target=process_message_background, args=(user_message, user_id))
    thread.daemon = True
    thread.start()


@app.route('/')
def index():
    return 'Additional Store LINE Webhook - 稼働中'


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
