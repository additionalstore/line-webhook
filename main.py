import os
import json
import smtplib
import socket
import threading
import traceback
import uuid
import urllib.request
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from flask import Flask, request, abort, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent, FollowEvent
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, MessagingApiBlob, ReplyMessageRequest, TextMessage
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

DESIGNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'designs')
os.makedirs(DESIGNS_DIR, exist_ok=True)

GREETING_TEXT = (
    "友だち追加ありがとうございます！Additional Store（高円寺の刺繍ファクトリー）です。\n\n"
    "🎁 今なら初回注文限定で「刺繍データ作成費」が半額になる特典中です（7/31まで・お一人様1回）\n\n"
    "ご注文の際に「LINE見ました」とお伝えください。店頭でもSquare通販でもご利用いただけます。\n\n"
    "手書きのイラストやスマホで撮った写真を送っていただくだけでも、データ化・仕様のご相談を承ります。お気軽にどうぞ！"
)

QUOTE_REQUEST_TRIGGER = "見積もりを依頼したいです"
QUOTE_REQUEST_AUTO_REPLY = (
    "お見積もりのご依頼ありがとうございます！\n\n"
    "🎁 今なら初回注文限定で「刺繍データ作成費」が半額になるキャンペーン中です（7/31まで・お一人様1回）\n\n"
    "担当よりあらためてご連絡いたしますので、少々お待ちください。お急ぎの場合は下記までお電話ください。\n"
    "📞ショップ：03-5913-7719\n"
    "📞ファクトリー直通：03-5364-9934"
)


def reply_line_message(reply_token, text):
    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
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

    to_addrs = ['miyata.4078@gmail.com']

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


def send_gmail_image_notification(image_bytes, user_id):
    """LINEで届いた画像を添付してGmail通知を送る"""
    gmail_user = os.environ.get('GMAIL_USER', '')
    gmail_app_password = os.environ.get('GMAIL_APP_PASSWORD', '')
    if not gmail_user or not gmail_app_password:
        print('Gmail通知エラー: GMAIL_USER または GMAIL_APP_PASSWORD が設定されていません')
        return

    to_addrs = ['miyata.4078@gmail.com']

    msg = MIMEMultipart()
    msg['Subject'] = '【LINE画像】お客様から画像が届きました'
    msg['From'] = gmail_user
    msg['To'] = ', '.join(to_addrs)

    body = f"""LINEに画像が届きました。

添付の画像を確認し、デザイン案の作成が必要であればClaude Codeに「この画像から刺繍デザイン案を作って」と伝えてください。

【ユーザーID】
{user_id}
"""
    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    image_part = MIMEImage(image_bytes)
    image_part.add_header('Content-Disposition', 'attachment', filename='line_image.jpg')
    msg.attach(image_part)

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as server:
            server.login(gmail_user, gmail_app_password)
            server.sendmail(gmail_user, to_addrs, msg.as_string())
        print('画像Gmail通知送信完了')
    except Exception as e:
        print(f'画像Gmail通知エラー: {type(e).__name__}: {e}')
        traceback.print_exc()


def process_image_message_background(message_id, user_id):
    print(f'画像メッセージ バックグラウンド処理開始: message_id={message_id}')
    try:
        with ApiClient(line_config) as api_client:
            image_bytes = MessagingApiBlob(api_client).get_message_content(message_id)
        print('画像取得完了')
    except Exception as e:
        print(f'画像取得エラー: {type(e).__name__}: {e}')
        traceback.print_exc()
        return

    send_gmail_image_notification(image_bytes, user_id)
    print('画像メッセージ バックグラウンド処理完了')


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

    if user_message == QUOTE_REQUEST_TRIGGER:
        try:
            reply_line_message(event.reply_token, QUOTE_REQUEST_AUTO_REPLY)
            print('お見積もり依頼の自動返信完了')
        except Exception as e:
            print(f'お見積もり依頼の自動返信エラー: {type(e).__name__}: {e}')
            traceback.print_exc()

    thread = threading.Thread(target=process_message_background, args=(user_message, user_id))
    thread.daemon = False
    thread.start()


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    message_id = event.message.id
    user_id = event.source.user_id
    print(f'画像受信: message_id={message_id}')

    thread = threading.Thread(target=process_image_message_background, args=(message_id, user_id))
    thread.daemon = False
    thread.start()


@handler.add(FollowEvent)
def handle_follow(event):
    print('=== 友だち追加イベント受信 ===')
    try:
        reply_line_message(event.reply_token, GREETING_TEXT)
        print('あいさつメッセージ送信完了')
    except Exception as e:
        print(f'あいさつメッセージ送信エラー: {type(e).__name__}: {e}')
        traceback.print_exc()


@app.route('/upload_design', methods=['POST'])
def upload_design():
    secret = request.headers.get('X-Upload-Secret', '')
    if not secret or secret != os.environ.get('DESIGN_UPLOAD_SECRET', ''):
        abort(401)

    image_bytes = request.get_data()
    if not image_bytes:
        return jsonify({'error': 'no image data'}), 400

    filename = secure_filename(f'{uuid.uuid4().hex}.png')
    with open(os.path.join(DESIGNS_DIR, filename), 'wb') as f:
        f.write(image_bytes)

    url = request.host_url.rstrip('/') + f'/designs/{filename}'
    return jsonify({'url': url})


@app.route('/designs/<filename>')
def get_design(filename):
    return send_from_directory(DESIGNS_DIR, secure_filename(filename))


@app.route('/')
def index():
    return 'Additional Store LINE Webhook - 稼働中'


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
