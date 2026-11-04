import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from datetime import datetime, timedelta
from urllib.parse import quote
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from threading import Thread

# --- RENDER KEEP-ALIVE SERVER ---
app = Flask('')
@app.route('/')
def home(): return "Bot is running and healthy!"

def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    Thread(target=run_web).start()

# --- CONFIGURATION (Environment Variables) ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
ADMIN_ID = int(os.getenv('ADMIN_ID'))
UPI_ID = os.getenv('UPI_ID')
CONTACT_USERNAME = os.getenv('CONTACT_USERNAME')

bot = telebot.TeleBot(BOT_TOKEN)
client = MongoClient(MONGO_URI)
db = client['sub_management']
channels_col = db['channels']
users_col = db['users']
coupons_col = db['coupons']
checkouts_col = db['checkouts']

# --- ADMIN LOGIC ---

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    text = message.text.split()

    # User entry via Deep Link
    if len(text) > 1:
        try:
            ch_id = int(text[1])
            ch_data = channels_col.find_one({"channel_id": ch_id})
            if ch_data:
                markup = InlineKeyboardMarkup()
                # Display Dynamic Plans
                for p_time, p_price in ch_data['plans'].items():
                    label = f"{p_time} Min" if int(p_time) < 60 else f"{int(p_time)//1440} Days"
                    markup.add(InlineKeyboardButton(f"💳 {label} - ₹{p_price}", callback_data=f"select_{ch_id}_{p_time}"))
                
                markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
                bot.send_message(message.chat.id, 
                    f"Welcome!\n\nYou are joining: *{ch_data['name']}*.\n\nPlease select a subscription plan below:", 
                    reply_markup=markup, parse_mode="Markdown")
                return
        except: pass

    # Admin Panel Greeting
    if user_id == ADMIN_ID:
        bot.send_message(message.chat.id, "✅ Admin Panel Active!\n\n/add - Add/Edit Channel & Prices\n/channels - Manage Existing Channels\n/coupons - Manage Discount Coupons")
    else:
        bot.send_message(message.chat.id, "Welcome! To join a channel, please use the link provided by the Admin.")

@bot.message_handler(commands=['channels'], func=lambda m: m.from_user.id == ADMIN_ID)
def list_channels(message):
    markup = InlineKeyboardMarkup()
    # Fetch all channels managed by this admin
    cursor = channels_col.find({"admin_id": ADMIN_ID})
    count = 0
    for ch in cursor:
        markup.add(InlineKeyboardButton(f"Channel: {ch['name']}", callback_data=f"manage_{ch['channel_id']}"))
        count += 1
    
    markup.add(InlineKeyboardButton("➕ Add New Channel", callback_data="add_new"))
    
    if count == 0:
        bot.send_message(ADMIN_ID, "No channels found. Click below to add one.", reply_markup=markup)
    else:
        bot.send_message(ADMIN_ID, "Your Managed Channels:", reply_markup=markup)

@bot.message_handler(commands=['add'], func=lambda m: m.from_user.id == ADMIN_ID)
def add_channel_start(message):
    msg = bot.send_message(ADMIN_ID, "Please ensure the bot is an Admin in your channel, then FORWARD any message from that channel here.")
    bot.register_next_step_handler(msg, get_plans)

# Callback for Add New button
@bot.callback_query_handler(func=lambda call: call.data == "add_new")
def cb_add_new(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(ADMIN_ID, "Please FORWARD any message from your channel here.")
    bot.register_next_step_handler(msg, get_plans)

def get_plans(message):
    if message.forward_from_chat:
        ch_id = message.forward_from_chat.id
        ch_name = message.forward_from_chat.title
        msg = bot.send_message(ADMIN_ID, 
            f"Channel Detected: *{ch_name}*\n\nEnter plans in format (Minutes:Price):\n`Min:Price, Min:Price` \n\n"
            "Example:\n`1440:99, 43200:199` (1 Day and 30 Days)", parse_mode="Markdown")
        bot.register_next_step_handler(msg, finalize_channel, ch_id, ch_name)
    else:
        bot.send_message(ADMIN_ID, "❌ Error: Message was not forwarded. Use /add to try again.")

def finalize_channel(message, ch_id, ch_name):
    try:
        raw_plans = message.text.split(',')
        plans_dict = {}
        for p in raw_plans:
            t, pr = p.strip().split(':')
            plans_dict[t] = pr
        
        channels_col.update_one({"channel_id": ch_id}, {"$set": {"name": ch_name, "plans": plans_dict, "admin_id": ADMIN_ID}}, upsert=True)
        bot_username = bot.get_me().username
        bot.send_message(ADMIN_ID, f"✅ Setup Successful!\n\nInvite Link for users:\n`https://t.me/{bot_username}?start={ch_id}`", parse_mode="Markdown")
    except:
        bot.send_message(ADMIN_ID, "❌ Invalid format. Please use `Min:Price, Min:Price`. Use /add to retry.")

# --- COUPON + USER PAYMENT FLOW ---

def get_plan_label(mins):
    mins = int(mins)
    return f"{mins} Min" if mins < 60 else f"{mins // 1440} Days"


def calculate_discount(price, percent):
    return round(float(price) * (100 - int(percent)) / 100, 2)


def make_upi_qr(price):
    upi_uri = f"upi://pay?pa={UPI_ID}&am={price:.2f}&cu=INR"
    return "https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=" + quote(upi_uri, safe="")


def show_payment_screen(chat_id, ch_id, mins, user_id, notice=None):
    checkout = checkouts_col.find_one({"user_id": user_id, "channel_id": ch_id, "plan": str(mins)})
    ch_data = channels_col.find_one({"channel_id": ch_id})
    if not checkout or not ch_data:
        bot.send_message(chat_id, "❌ Checkout session nahi mila. Please plan dobara select karein.")
        return

    original = float(checkout["original_price"])
    final_price = float(checkout["final_price"])
    discount = round(original - final_price, 2)
    caption = (
        f"Plan: {get_plan_label(mins)}\n"
        f"Original Price: ₹{original:.2f}\n"
    )
    if checkout.get("coupon_code"):
        caption += f"Coupon: {checkout['coupon_code']} ({checkout['discount_percent']}% OFF)\nDiscount: -₹{discount:.2f}\n"
    caption += f"Final Amount: ₹{final_price:.2f}\nUPI ID: {UPI_ID}\n\nPlease complete the payment and click 'I Have Paid'."
    if notice:
        caption = notice + "\n\n" + caption

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🎟️ Apply Coupon", callback_data=f"coupon_{ch_id}_{mins}"))
    markup.add(InlineKeyboardButton("🔄 Regenerate Payment QR", callback_data=f"regen_{ch_id}_{mins}"))
    markup.add(InlineKeyboardButton("✅ I Have Paid", callback_data=f"paid_{ch_id}_{mins}"))
    markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
    bot.send_photo(chat_id, make_upi_qr(final_price), caption=caption, reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('select_'))
def user_pays(call):
    try:
        _, ch_id, mins = call.data.split('_')
        ch_id = int(ch_id)
        ch_data = channels_col.find_one({"channel_id": ch_id})
        if not ch_data or mins not in ch_data.get('plans', {}):
            bot.answer_callback_query(call.id, "Plan not found.", show_alert=True)
            return
        price = float(ch_data['plans'][mins])
        checkouts_col.update_one(
            {"user_id": call.from_user.id, "channel_id": ch_id, "plan": str(mins)},
            {"$set": {"original_price": price, "final_price": price, "coupon_code": None,
                      "discount_percent": 0, "updated_at": datetime.utcnow()}}, upsert=True
        )
        bot.answer_callback_query(call.id)
        show_payment_screen(call.message.chat.id, ch_id, mins, call.from_user.id)
    except Exception as e:
        bot.answer_callback_query(call.id, "Unable to open payment details.", show_alert=True)
        bot.send_message(ADMIN_ID, f"Payment screen error: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith('coupon_'))
def ask_coupon_code(call):
    _, ch_id, mins = call.data.split('_')
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "🎟️ Apna coupon code bhejiye:")
    bot.register_next_step_handler(msg, apply_coupon_code, int(ch_id), str(mins))


def apply_coupon_code(message, ch_id, mins):
    code = (message.text or "").strip().upper()
    coupon = coupons_col.find_one({"code": code, "active": True, "channel_id": ch_id})
    checkout = checkouts_col.find_one({"user_id": message.from_user.id, "channel_id": ch_id, "plan": mins})
    ch_data = channels_col.find_one({"channel_id": ch_id})

    if not checkout or not ch_data:
        bot.send_message(message.chat.id, "❌ Payment session expire ho gaya. Please plan dobara select karein.")
        return
    if not coupon:
        bot.send_message(message.chat.id, "❌ Invalid ya inactive coupon code. Channel ke liye coupon check karein.")
        show_payment_screen(message.chat.id, ch_id, mins, message.from_user.id)
        return
    allowed_plans = coupon.get("plans", [])
    if allowed_plans and mins not in allowed_plans:
        bot.send_message(message.chat.id, "❌ Ye coupon is subscription plan par valid nahi hai.")
        show_payment_screen(message.chat.id, ch_id, mins, message.from_user.id)
        return

    percent = int(coupon["discount_percent"])
    final_price = calculate_discount(checkout["original_price"], percent)
    if final_price <= 0:
        bot.send_message(message.chat.id, "❌ Coupon configuration invalid hai. Admin se contact karein.")
        return
    checkouts_col.update_one(
        {"user_id": message.from_user.id, "channel_id": ch_id, "plan": mins},
        {"$set": {"final_price": final_price, "coupon_code": code,
                  "discount_percent": percent, "updated_at": datetime.utcnow()}}
    )
    bot.send_message(message.chat.id, f"✅ Coupon applied! Aapko {percent}% discount mila.")
    show_payment_screen(message.chat.id, ch_id, mins, message.from_user.id, "🎉 Discount successfully applied!")


@bot.callback_query_handler(func=lambda call: call.data.startswith('regen_'))
def regenerate_payment_qr(call):
    _, ch_id, mins = call.data.split('_')
    bot.answer_callback_query(call.id, "Payment QR refreshed.")
    show_payment_screen(call.message.chat.id, int(ch_id), mins, call.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('paid_'))
def admin_notify(call):
    _, ch_id, mins = call.data.split('_')
    ch_id = int(ch_id)
    user = call.from_user
    ch_data = channels_col.find_one({"channel_id": ch_id})
    checkout = checkouts_col.find_one({"user_id": user.id, "channel_id": ch_id, "plan": str(mins)})
    if not ch_data or not checkout:
        bot.answer_callback_query(call.id, "Payment details not found. Select the plan again.", show_alert=True)
        return

    # Prevent duplicate pending payment requests for the same checkout.
    request_id = f"{user.id}_{ch_id}_{mins}"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Approve", callback_data=f"app_{user.id}_{ch_id}_{mins}"))
    markup.add(InlineKeyboardButton("❌ Reject", callback_data=f"rej_{user.id}"))

    coupon_line = f"\nCoupon: {checkout.get('coupon_code')} ({checkout.get('discount_percent', 0)}% OFF)" if checkout.get('coupon_code') else ""
    bot.send_message(
        ADMIN_ID,
        f"🔔 Payment Verification Required!\n\n"
        f"User: {user.first_name} (@{user.username or 'no_username'})\n"
        f"User ID: {user.id}\n"
        f"Channel: {ch_data['name']}\n"
        f"Plan: {get_plan_label(mins)}\n"
        f"Original Price: ₹{float(checkout['original_price']):.2f}"
        f"{coupon_line}\n"
        f"Final Amount to verify: ₹{float(checkout['final_price']):.2f}",
        reply_markup=markup
    )
    bot.answer_callback_query(call.id, "Request sent to Admin.")
    u_markup = InlineKeyboardMarkup().add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
    bot.send_message(call.message.chat.id, "✅ Your payment request has been sent. Please wait for Admin approval.", reply_markup=u_markup)

# --- APPROVAL & EXPIRY ---

@bot.callback_query_handler(func=lambda call: call.data.startswith('app_'))
def approve_now(call):
    _, u_id, ch_id, mins = call.data.split('_')
    u_id, ch_id, mins = int(u_id), int(ch_id), int(mins)
    
    try:
        expiry_datetime = datetime.now() + timedelta(minutes=mins)
        expiry_ts = int(expiry_datetime.timestamp())

        # Link expires when sub ends
        link = bot.create_chat_invite_link(ch_id, member_limit=1, expire_date=expiry_ts)
        
        users_col.update_one({"user_id": u_id, "channel_id": ch_id}, {"$set": {"expiry": expiry_datetime.timestamp()}}, upsert=True)
        checkout = checkouts_col.find_one({"user_id": u_id, "channel_id": ch_id, "plan": str(mins)})
        if checkout and checkout.get("coupon_code"):
            coupons_col.update_one({"code": checkout["coupon_code"], "channel_id": ch_id}, {"$inc": {"used_count": 1}})
        
        bot.send_message(u_id, f"🥳 *Payment Approved!*\n\nSubscription: {mins} Minutes\n\nJoin Link: {link.invite_link}\n\n⚠️ Note: This link and your access will expire in {mins} minutes.", parse_mode="Markdown")
        bot.edit_message_text(f"✅ Approved user {u_id} for {mins} mins.", call.message.chat.id, call.message.message_id)
        
    except Exception as e:
        bot.send_message(ADMIN_ID, f"❌ Error: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('rej_'))
def reject_payment(call):
    u_id = int(call.data.split('_')[1])
    bot.answer_callback_query(call.id, "Payment request rejected.")
    try:
        bot.send_message(
            u_id,
            "Kya yaar payment na karke bol rhe ho ki kardi sharam karo thoda 😔"
        )
        bot.edit_message_text(
            f"❌ Rejected payment request from user {u_id}.",
            call.message.chat.id,
            call.message.message_id
        )
    except Exception as e:
        bot.send_message(ADMIN_ID, f"❌ Rejection notification error for user {u_id}: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith('manage_'))
def manage_ch(call):
    ch_id = int(call.data.split('_')[1])
    ch_data = channels_col.find_one({"channel_id": ch_id})
    bot_username = bot.get_me().username
    link = f"https://t.me/{bot_username}?start={ch_id}"
    
    bot.edit_message_text(f"Settings for: *{ch_data['name']}*\n\nYour Link: `{link}`\n\nTo edit prices, use /add and forward a message from this channel again.", 
                          call.message.chat.id, call.message.message_id, parse_mode="Markdown")

# --- ADMIN COUPON MANAGEMENT ---

@bot.message_handler(commands=['coupons'], func=lambda m: m.from_user.id == ADMIN_ID)
def coupon_admin_panel(message):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("➕ Create Coupon", callback_data="cp_create"))
    markup.add(InlineKeyboardButton("📋 Manage Coupons", callback_data="cp_list"))
    bot.send_message(ADMIN_ID, "🎟️ Coupon Management\n\nCreate coupons for a specific channel and selected subscription plans.", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == 'cp_create')
def coupon_choose_channel(call):
    bot.answer_callback_query(call.id)
    markup = InlineKeyboardMarkup()
    channels = list(channels_col.find({"admin_id": ADMIN_ID}))
    if not channels:
        bot.send_message(ADMIN_ID, "❌ No channels found. First add a channel using /add.")
        return
    for ch in channels:
        markup.add(InlineKeyboardButton(ch['name'][:50], callback_data=f"cp_ch_{ch['channel_id']}"))
    bot.send_message(ADMIN_ID, "Select the channel for this coupon:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('cp_ch_'))
def coupon_get_code(call):
    ch_id = int(call.data.split('_')[2])
    ch_data = channels_col.find_one({"channel_id": ch_id, "admin_id": ADMIN_ID})
    if not ch_data:
        bot.answer_callback_query(call.id, "Channel not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    msg = bot.send_message(ADMIN_ID, f"Channel: {ch_data['name']}\nEnter coupon code (letters/numbers, no spaces):")
    bot.register_next_step_handler(msg, coupon_get_discount, ch_id)


def coupon_get_discount(message, ch_id):
    code = (message.text or '').strip().upper()
    if not code or len(code) > 24 or not code.isalnum():
        msg = bot.send_message(ADMIN_ID, "❌ Invalid code. Use letters, numbers or underscore only. Enter coupon code again:")
        bot.register_next_step_handler(msg, coupon_get_discount, ch_id)
        return
    if coupons_col.find_one({"code": code, "channel_id": ch_id}):
        msg = bot.send_message(ADMIN_ID, "❌ This coupon already exists for this channel. Enter a different code:")
        bot.register_next_step_handler(msg, coupon_get_discount, ch_id)
        return
    msg = bot.send_message(ADMIN_ID, "Enter discount percentage (1 to 90):")
    bot.register_next_step_handler(msg, coupon_choose_plans, ch_id, code)


def coupon_choose_plans(message, ch_id, code):
    try:
        percent = int((message.text or '').strip().replace('%', ''))
        if not 1 <= percent <= 90:
            raise ValueError
    except ValueError:
        msg = bot.send_message(ADMIN_ID, "❌ Enter a whole number between 1 and 90:")
        bot.register_next_step_handler(msg, coupon_choose_plans, ch_id, code)
        return
    ch_data = channels_col.find_one({"channel_id": ch_id})
    if not ch_data:
        bot.send_message(ADMIN_ID, "❌ Channel not found. Please start again with /coupons.")
        return
    plans = list(ch_data.get('plans', {}).keys())
    if not plans:
        bot.send_message(ADMIN_ID, "❌ No plans found for this channel.")
        return
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ All Plans", callback_data=f"cp_all_{ch_id}_{code}_{percent}"))
    for plan in plans:
        markup.add(InlineKeyboardButton(f"{get_plan_label(plan)} - ₹{ch_data['plans'][plan]}", callback_data=f"cp_plan_{ch_id}_{code}_{percent}_{plan}"))
    bot.send_message(ADMIN_ID, f"Coupon: {code} | Discount: {percent}%\nChoose where it can be used:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('cp_all_'))
def coupon_save_all_plans(call):
    _, _, ch_id, code, percent = call.data.split('_')
    save_coupon(call, int(ch_id), code, int(percent), [])


@bot.callback_query_handler(func=lambda call: call.data.startswith('cp_plan_'))
def coupon_save_one_plan(call):
    _, _, ch_id, code, percent, plan = call.data.split('_')
    save_coupon(call, int(ch_id), code, int(percent), [str(plan)])


def save_coupon(call, ch_id, code, percent, plans):
    ch_data = channels_col.find_one({"channel_id": ch_id, "admin_id": ADMIN_ID})
    if not ch_data:
        bot.answer_callback_query(call.id, "Channel not found.", show_alert=True)
        return
    coupons_col.insert_one({
        "code": code, "channel_id": ch_id, "channel_name": ch_data['name'],
        "discount_percent": percent, "plans": plans, "active": True,
        "used_count": 0, "created_at": datetime.utcnow()
    })
    bot.answer_callback_query(call.id, "Coupon created!")
    scope = "All plans" if not plans else ', '.join(get_plan_label(p) for p in plans)
    bot.send_message(ADMIN_ID, f"✅ Coupon Created\n\nCode: {code}\nChannel: {ch_data['name']}\nDiscount: {percent}%\nValid Plans: {scope}\nStatus: Active")


@bot.callback_query_handler(func=lambda call: call.data == 'cp_list')
def coupon_list(call):
    bot.answer_callback_query(call.id)
    coupons = list(coupons_col.find({"channel_id": {"$in": [c['channel_id'] for c in channels_col.find({"admin_id": ADMIN_ID}, {"channel_id": 1})]}}))
    if not coupons:
        bot.send_message(ADMIN_ID, "No coupons created yet. Use /coupons to create one.")
        return
    for cp in coupons:
        plans = "All plans" if not cp.get('plans') else ', '.join(get_plan_label(p) for p in cp['plans'])
        status = "🟢 Active" if cp.get('active', True) else "🔴 Disabled"
        markup = InlineKeyboardMarkup()
        toggle_label = "🚫 Disable" if cp.get('active', True) else "✅ Enable"
        markup.add(InlineKeyboardButton(toggle_label, callback_data=f"cp_toggle_{cp['channel_id']}_{cp['code']}"))
        markup.add(InlineKeyboardButton("✏️ Edit Discount", callback_data=f"cp_edit_{cp['channel_id']}_{cp['code']}"))
        markup.add(InlineKeyboardButton("🗑️ Delete", callback_data=f"cp_delete_{cp['channel_id']}_{cp['code']}"))
        bot.send_message(ADMIN_ID, f"🎟️ {cp['code']}\nChannel: {cp.get('channel_name', cp['channel_id'])}\nDiscount: {cp['discount_percent']}%\nPlans: {plans}\nUsed: {cp.get('used_count', 0)} times\nStatus: {status}", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('cp_toggle_'))
def coupon_toggle(call):
    _, _, ch_id, code = call.data.split('_', 3)
    cp = coupons_col.find_one({"channel_id": int(ch_id), "code": code})
    if not cp:
        bot.answer_callback_query(call.id, "Coupon not found.", show_alert=True)
        return
    new_status = not cp.get('active', True)
    coupons_col.update_one({"_id": cp['_id']}, {"$set": {"active": new_status}})
    bot.answer_callback_query(call.id, "Coupon status updated.")
    bot.send_message(ADMIN_ID, f"{code}: {'Enabled' if new_status else 'Disabled'}")


@bot.callback_query_handler(func=lambda call: call.data.startswith('cp_edit_'))
def coupon_edit_start(call):
    _, _, ch_id, code = call.data.split('_', 3)
    cp = coupons_col.find_one({"channel_id": int(ch_id), "code": code})
    if not cp:
        bot.answer_callback_query(call.id, "Coupon not found.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    msg = bot.send_message(ADMIN_ID, f"Enter new discount percentage for {code} (1 to 90):")
    bot.register_next_step_handler(msg, coupon_edit_save, int(ch_id), code)


def coupon_edit_save(message, ch_id, code):
    try:
        percent = int((message.text or '').strip().replace('%', ''))
        if not 1 <= percent <= 90:
            raise ValueError
    except ValueError:
        msg = bot.send_message(ADMIN_ID, "❌ Enter a whole number between 1 and 90:")
        bot.register_next_step_handler(msg, coupon_edit_save, ch_id, code)
        return
    coupons_col.update_one({"channel_id": ch_id, "code": code}, {"$set": {"discount_percent": percent}})
    bot.send_message(ADMIN_ID, f"✅ {code} discount updated to {percent}%.")


@bot.callback_query_handler(func=lambda call: call.data.startswith('cp_delete_'))
def coupon_delete(call):
    _, _, ch_id, code = call.data.split('_', 3)
    result = coupons_col.delete_one({"channel_id": int(ch_id), "code": code})
    bot.answer_callback_query(call.id, "Coupon deleted." if result.deleted_count else "Coupon not found.")
    if result.deleted_count:
        bot.send_message(ADMIN_ID, f"🗑️ Coupon {code} deleted.")


# Automate Kicking
def kick_expired_users():
    now = datetime.now().timestamp()
    expired_users = users_col.find({"expiry": {"$lte": now}})
    bot_username = bot.get_me().username

    for user in expired_users:
        try:
            bot.ban_chat_member(user['channel_id'], user['user_id'])
            bot.unban_chat_member(user['channel_id'], user['user_id'])
            
            rejoin_url = f"https://t.me/{bot_username}?start={user['channel_id']}"
            markup = InlineKeyboardMarkup().add(InlineKeyboardButton("🔄 Re-join / Renew", url=rejoin_url))
            
            bot.send_message(user['user_id'], "⚠️ Your subscription has expired.\n\nTo join again or renew, please click the button below:", reply_markup=markup)
            users_col.delete_one({"_id": user['_id']})
        except: pass

# --- STARTUP ---
if __name__ == '__main__':
    keep_alive()
    scheduler = BackgroundScheduler()
    scheduler.add_job(kick_expired_users, 'interval', minutes=1)
    scheduler.start()
    bot.remove_webhook()
    print("Bot is running...")
    bot.infinity_polling(timeout=20, long_polling_timeout=10)
        
