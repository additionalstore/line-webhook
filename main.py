import os
import json
import smtplib
import socket
import threading
import time
import traceback
import uuid
import urllib.request
import imaplib
import email as email_module
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from flask import Flask, request, abort, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent, FollowEvent
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, MessagingApiBlob, ReplyMessageRequest, PushMessageRequest, TextMessage
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

# ユーザーごとの簡易会話履歴（プロセス起動中のみ保持。再デプロイ・スリープで消える）
CONVERSATION_HISTORY = {}
HISTORY_MAX_TURNS = 20


def append_history(user_id, role, text):
    history = CONVERSATION_HISTORY.setdefault(user_id, [])
    if history and history[-1]['role'] == role:
        history[-1]['content'] += '\n' + text
    else:
        history.append({'role': role, 'content': text})
    if len(history) > HISTORY_MAX_TURNS:
        del history[:-HISTORY_MAX_TURNS]

GREETING_TEXT = (
    "友だち追加ありがとうございます！Additional Store（高円寺の刺繍ファクトリー）です。\n\n"
    "手書きのイラストやスマホで撮った写真を送っていただくだけでも、データ化・仕様のご相談を承ります。お気軽にどうぞ！"
)

QUOTE_REQUEST_TRIGGER = "見積もりを依頼したいです"
QUOTE_REQUEST_AUTO_REPLY = (
    "お見積もりのご依頼ありがとうございます！\n\n"
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


def push_line_message(user_id, text):
    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=user_id, messages=[TextMessage(text=text)])
        )


def get_line_display_name(user_id):
    """LINEの表示名を取得する。友だち解除済みなどで取得できない場合は空文字を返す"""
    try:
        with ApiClient(line_config) as api_client:
            profile = MessagingApi(api_client).get_profile(user_id)
        return profile.display_name
    except Exception as e:
        print(f'プロフィール取得エラー: {type(e).__name__}: {e}')
        return ''


# この顧客は社長が手入力で直接返信する方針のため、メール返信からの自動送信対象から除外する
EMAIL_AUTO_SEND_EXCLUDED_USER_IDS = {'Ua3863c0363d993d10ddbec17de1072ad'}

SYSTEM_PROMPT = """
あなたは「有限会社サラ（Additional Store）」のカスタマーサポート担当です。
以下の会社情報をもとに、丁寧で簡潔な返信メッセージの案を作成してください。

【会社情報】
- 会社名: 有限会社サラ / Additional Store
- 所在地: 東京都高円寺
- 事業内容: 国内自社ファクトリーによる刺繍加工専門・アパレルOEM・グッズODM・ノベルティ制作（プリント加工は行っていない）
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
- 当社は刺繍加工専門。「刺繍/プリント」のように加工方法を選択肢として提示しない。加工内容を聞く場合は「刺繍位置・デザインイメージ」等、刺繍前提の聞き方にする
"""


def generate_reply(user_id, user_message):
    api_key = os.environ['ANTHROPIC_API_KEY'].strip()
    messages = CONVERSATION_HISTORY.get(user_id) or [{'role': 'user', 'content': user_message}]
    payload = json.dumps({
        'model': 'claude-haiku-4-5-20251001',
        'max_tokens': 512,
        'system': SYSTEM_PROMPT,
        'messages': messages
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


def send_gmail_notification(user_message, reply_suggestion, user_id, display_name=''):
    gmail_user = os.environ.get('GMAIL_USER', '')
    gmail_app_password = os.environ.get('GMAIL_APP_PASSWORD', '')
    if not gmail_user or not gmail_app_password:
        print('Gmail通知エラー: GMAIL_USER または GMAIL_APP_PASSWORD が設定されていません')
        return

    subject = f'【LINE返信案】{user_message[:20]}...'
    body = f"""LINEにメッセージが届きました。

【LINE表示名】
{display_name or '(取得できませんでした)'}

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


def send_gmail_image_notification(image_bytes, user_id, display_name=''):
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

【LINE表示名】
{display_name or '(取得できませんでした)'}

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
    display_name = get_line_display_name(user_id)
    try:
        with ApiClient(line_config) as api_client:
            image_bytes = MessagingApiBlob(api_client).get_message_content(message_id)
        print('画像取得完了')
    except Exception as e:
        print(f'画像取得エラー: {type(e).__name__}: {e}')
        traceback.print_exc()
        return

    send_gmail_image_notification(image_bytes, user_id, display_name)
    print('画像メッセージ バックグラウンド処理完了')


def process_message_background(user_message, user_id):
    print(f'バックグラウンド処理開始: {user_message[:30]}')
    display_name = get_line_display_name(user_id)
    try:
        reply_suggestion = generate_reply(user_id, user_message)
        print('Claude返信案生成完了')
    except Exception as e:
        print(f'Claude APIエラー: {type(e).__name__}: {e}')
        traceback.print_exc()
        reply_suggestion = '（返信案の生成に失敗しました）'

    send_gmail_notification(user_message, reply_suggestion, user_id, display_name)
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


@app.route('/send_reply', methods=['POST'])
def send_reply():
    """外出先などローカルPCなしでLINE返信を送るためのエンドポイント。
    社長の明示確認を得たうえでClaudeが叩く想定（勝手な自動送信はしないこと）。"""
    secret = request.headers.get('X-Send-Secret', '')
    if not secret or secret != os.environ.get('SEND_REPLY_SECRET', ''):
        abort(401)

    data = request.get_json(silent=True) or {}
    user_id = data.get('user_id', '')
    message = data.get('message', '')
    if not user_id or not message:
        return jsonify({'error': 'user_id and message are required'}), 400

    try:
        push_line_message(user_id, message)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500

    return jsonify({'status': 'sent'})


@app.route('/')
def index():
    return 'Additional Store LINE Webhook - 稼働中'


# ===== 通知メールへの返信を検知して自動送信する仕組み（2026-07-08追加） =====
# 社長が外出先などで通知メール（【LINE返信案】...）にそのまま返信すると、
# Claudeが作った返信案をLINEへ自動送信する。内容の書き換えには対応せず、
# 「返信＝下書きをそのまま送る」というシンプルな承認シグナルとしてのみ扱う
# （引用元テキストの誤解釈で客に変な文面を送るリスクを避けるため）。
# 込み入った相談はこのフローを使わず、Claude Codeとチャットして通常の
# send_reply経由で送ることを想定。

REPLY_POLL_INTERVAL_SECONDS = 150
NOTIFICATION_SUBJECT_MARKER = '【LINE返信案】'
DRAFT_MARKER = '【返信案（Claude生成）】'
USER_ID_MARKER = '【ユーザーID】'
AUTO_SENT_LABEL = 'claude-auto-sent'


def _get_plain_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            content_disposition = str(part.get('Content-Disposition', ''))
            if part.get_content_type() == 'text/plain' and 'attachment' not in content_disposition:
                charset = part.get_content_charset() or 'utf-8'
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(charset, errors='replace')
        return ''
    charset = msg.get_content_charset() or 'utf-8'
    payload = msg.get_payload(decode=True)
    return payload.decode(charset, errors='replace') if payload else ''


def _extract_draft_and_user_id(body):
    """引用された元の通知メール本文から、Claudeの返信案とuser_idを抜き出す"""
    draft = ''
    if DRAFT_MARKER in body and USER_ID_MARKER in body:
        draft_block = body.split(DRAFT_MARKER, 1)[1].split(USER_ID_MARKER, 1)[0]
        draft = '\n'.join(
            line.lstrip('> ').rstrip() for line in draft_block.splitlines()
        ).strip()

    user_id = ''
    if USER_ID_MARKER in body:
        after = body.split(USER_ID_MARKER, 1)[1]
        for line in after.splitlines():
            line = line.lstrip('> ').strip()
            if line:
                user_id = line
                break

    return draft, user_id


def check_and_send_email_replies():
    gmail_user = os.environ.get('GMAIL_USER', '')
    gmail_app_password = os.environ.get('GMAIL_APP_PASSWORD', '')
    if not gmail_user or not gmail_app_password:
        return

    with imaplib.IMAP4_SSL('imap.gmail.com', timeout=20) as imap:
        imap.login(gmail_user, gmail_app_password)
        imap.select('INBOX')
        # 日付範囲を必ず絞ること（絞らないと何年も前の無関係な「Re: ...」メールまで大量にヒットして誤送信しかねない）
        query = f'subject:"Re: {NOTIFICATION_SUBJECT_MARKER}" -label:{AUTO_SENT_LABEL} newer_than:3d'
        status, data = imap.uid('search', None, 'X-GM-RAW', f'"{query}"')
        if status != 'OK' or not data or not data[0]:
            return

        for uid in data[0].split():
            status, msg_data = imap.uid('fetch', uid, '(BODY.PEEK[])')
            if status != 'OK' or not msg_data or not msg_data[0]:
                continue

            msg = email_module.message_from_bytes(msg_data[0][1])
            body = _get_plain_body(msg)
            draft, user_id = _extract_draft_and_user_id(body)

            if not user_id or not draft:
                print(f'返信メール処理スキップ（draft/user_id抽出失敗）: uid={uid}')
                imap.uid('store', uid, '+X-GM-LABELS', f'({AUTO_SENT_LABEL})')
                continue

            if user_id in EMAIL_AUTO_SEND_EXCLUDED_USER_IDS:
                print(f'返信メール処理スキップ（手入力対応の顧客）: user_id={user_id}')
                imap.uid('store', uid, '+X-GM-LABELS', f'({AUTO_SENT_LABEL})')
                continue

            try:
                push_line_message(user_id, draft)
                print(f'メール返信からLINE自動送信完了: user_id={user_id}')
            except Exception as e:
                print(f'メール返信からのLINE送信エラー: {type(e).__name__}: {e}')
                traceback.print_exc()
                continue

            imap.uid('store', uid, '+X-GM-LABELS', f'({AUTO_SENT_LABEL})')


def _email_reply_polling_loop():
    while True:
        time.sleep(REPLY_POLL_INTERVAL_SECONDS)
        try:
            check_and_send_email_replies()
        except Exception as e:
            print(f'メール返信チェックループエラー: {type(e).__name__}: {e}')
            traceback.print_exc()


threading.Thread(target=_email_reply_polling_loop, daemon=True).start()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
