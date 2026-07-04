import os
import json
import smtplib
import socket
import threading
import traceback
import urllib.request
from email.mime.text import MIMEText
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from dotenv import load_dotenv

# Render.com ではIPv6が使えないためIPv4のみ使用する
_orig_getaddrinfo = socket.getaddrinfo
def _getaddrinfo_ipv4(*args, **kwargs):
    results = _orig_getaddrinfo(*args, **kwargs)
    ipv4 = [r for r in results if r[0] == socket.AF_INET]
    return ipv4 if ipv4 else results
socket.getaddrinfo = _getaddrinfo_ipv4

load_dotenv()

app = Flask(__name__)

handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])
line_config = Configuration(access_token=os.environ['LINE_CHANNEL_ACCESS_TOKEN'])

GREETING_TEXT = (
    "友だち追加ありがとうございます！Additional Store（高円寺の刺繍ファクトリー）です。\n\n"
    "🎁 今なら初回注文限定で「刺繍データ作成費」が半額になる特典中です（7/31まで・お一人様1回）\n\n"
    "ご注文の際に「LINE見ました」とお伝えください。店頭でもSquare通販でもご利用いただけます。\n\n"
    "手書きのイラストやスマホで撮った写真を送っていただくだけでも、データ化・仕様のご相談を承ります。お気軽にどうぞ！"
)

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
    api_key = os.environ['ANTHROPIC_API_KEY'].strip()
    payload = json.dumps({
        'model': 'claude-haiku-4-5-20251001',
        'max_tokens': 512,
        'system': SYSTEM_PROMPT,
        'messages': [{'role': 'user', 'content': user_message}]
    }).encode('utf-8')

    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=payload,
        method='POST'
    )
    req.add_header('x-api-key', api_key)
    req.add_header('anthropic-version', '2023-06-01')
    req.add_header('content-type', 'application/json')

    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result['content'][0]['text']


def send_gmail_notification(user_message, reply_suggestion, user_id):
    gmail_user = os.environ.get('GMAIL_USER', '')
    gmail_app_password = os.environ.get('GMAIL_APP_PASSWORD', '')
    if not gmail_user or not gmail_app_password:
        print('Gmail通知エラー: GMAIL_USER または GMAIL_APP_PASSWORD が設定されていません')
        return

    subject = f'【LINE返信案】{user_message[:20]}...'
    body = f"""LINEにメッセージが届きました。

【受信メッセージ】
{user_message}

【返信案（Claude生成）】
{reply_suggestion}

【ユーザーID】
{user_id}

---
LINE Official Account Manager で確認・送信してください:
https://manager.line.biz/

Claude Codeに直接返信させる場合は、このメール本文をそのままチャットに貼ってください。
"""

    to_addrs = ['miyata.4078@gmail.com', 'ji24miyata@gmail.com']

    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = gmail_user
    msg['To'] = ', '.join(to_addrs)

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as server:
            server.login(gmail_user, gmail_app_password)
            server.sendmail(gmail_user, to_addrs, msg.as_string())
        print('Gmail通知送信完了')
    except Exception as e:
        print(f'Gmail通知エラー: {type(e).__name__}: {e}')
        traceback.print_exc()


def process_message_background(user_message, user_id):
    print(f'バックグラウンド処理開始: {user_message[:30]}')
    try:
        reply_suggestion = generate_reply(user_message)
        print('Claude返信案生成完了')
    except Exception as e:
        print(f'Claude APIエラー: {type(e).__name__}: {e}')
        traceback.print_exc()
        reply_suggestion = '（返信案の生成に失敗しました）'

    send_gmail_notification(user_message, reply_suggestion, user_id)
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

    thread = threading.Thread(target=process_message_background, args=(user_message, user_id))
    thread.daemon = False
    thread.start()


@handler.add(FollowEvent)
def handle_follow(event):
    print('=== 友だち追加イベント受信 ===')
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=GREETING_TEXT)],
                )
            )
        print('あいさつメッセージ送信完了')
    except Exception as e:
        print(f'あいさつメッセージ送信エラー: {type(e).__name__}: {e}')
        traceback.print_exc()


@app.route('/')
def index():
    return 'Additional Store LINE Webhook - 稼働中'


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
