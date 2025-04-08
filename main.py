import json
import logging
import os
import requests
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (Updater, CommandHandler, MessageHandler, Filters,
                          ConversationHandler, CallbackContext)

# Setup logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
HOME, WORK, DEPART_HOME, DEPART_WORK = range(4)

# Global settings (saved as a JSON file)
SETTINGS_FILE = 'settings.json'
user_settings = {}

# Environment Variables – these will be set in Railway
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
OWM_API_KEY = os.environ.get('OWM_API_KEY')        # OpenWeatherMap API key
GMAPS_API_KEY = os.environ.get('GMAPS_API_KEY')      # Google Maps API key

# ------------------ Helper Functions ------------------
def save_settings():
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(user_settings, f)

def load_settings():
    global user_settings
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f:
            user_settings = json.load(f)
    else:
        user_settings = {}

def get_weather(lat: float, lon: float, retries=3):
    url = f"http://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OWM_API_KEY}&units=metric"
    attempt = 0
    while attempt < retries:
        try:
            response = requests.get(url, timeout=10)
            data = response.json()
            if response.status_code == 200:
                temperature = data["main"]["temp"]
                humidity = data["main"]["humidity"]
                # Simple logic: if 'rain' exists in description, we assume 100% chance.
                weather_desc = data["weather"][0]["description"]
                chance_of_rain = 100 if "rain" in weather_desc.lower() else 0
                return temperature, humidity, chance_of_rain, weather_desc
            else:
                attempt += 1
        except Exception as e:
            logger.error(f"Weather API error: {e}")
            attempt += 1
    return None, None, None, "Unable to fetch weather data"

def get_travel_time(origin: str, destination: str, departure_time: int, retries=3):
    # departure_time is a Unix timestamp in seconds.
    url = f"https://maps.googleapis.com/maps/api/distancematrix/json?origins={origin}&destinations={destination}&departure_time={departure_time}&key={GMAPS_API_KEY}"
    attempt = 0
    while attempt < retries:
        try:
            response = requests.get(url, timeout=10)
            data = response.json()
            if data["status"] == "OK":
                element = data["rows"][0]["elements"][0]
                if element["status"] == "OK":
                    duration_in_traffic = element["duration_in_traffic"]["value"]
                    minutes = int(duration_in_traffic / 60)
                    return minutes
                else:
                    attempt += 1
            else:
                attempt += 1
        except Exception as e:
            logger.error(f"Traffic API error: {e}")
            attempt += 1
    return None

def schedule_notifications(context: CallbackContext):
    """Schedules daily notifications for Home→Work and Work→Home."""
    scheduler = BackgroundScheduler()
    now = datetime.now()

    # Helper to add job
    def add_job(dept_time_str, job_func, job_name):
        try:
            dept_time = datetime.strptime(dept_time_str, "%H:%M").time()
        except ValueError:
            logger.error("Invalid time format in settings.")
            return

        scheduled_time = datetime.combine(now.date(), dept_time)
        if scheduled_time < now:
            scheduled_time += timedelta(days=1)
        scheduler.add_job(job_func, 'date', run_date=scheduled_time, id=job_name)
        logger.info(f"Scheduled {job_name} for {scheduled_time}")

    # Job functions for notifications
    def job_departure_home():
        departure_ts = int(datetime.now().timestamp())
        minutes = get_travel_time(user_settings["home_address"], user_settings["work_address"], departure_ts)
        arrival_time = (datetime.now() + timedelta(minutes=minutes)).strftime("%H:%M") if minutes else "unknown"
        weather = get_weather(user_settings["home_lat"], user_settings["home_lon"])
        message = f"Time to go! Expect a {minutes} minute drive to Work. Arrival is expected at {arrival_time}.\n"
        if weather[0] is not None:
            message += f"Weather: {weather[0]}°C, {weather[3]}, Humidity: {weather[1]}%, Chance of rain: {weather[2]}%."
            if weather[2] > 0:
                message += " Tip: Wear a waterproof jacket/umbrella."
        else:
            message += "Weather data unavailable."
        context.bot.send_message(chat_id=user_settings["chat_id"], text=message)

    def job_pre_departure_home():
        departure_ts = int((datetime.now() + timedelta(minutes=30)).timestamp())
        minutes = get_travel_time(user_settings["home_address"], user_settings["work_address"], departure_ts)
        weather = get_weather(user_settings["home_lat"], user_settings["home_lon"])
        message = f"Plan to leave by {user_settings['depart_home']} to Work.\nPredicted travel time: {minutes} minutes.\n"
        if weather[0] is not None:
            message += f"Weather: {weather[0]}°C, {weather[3]}, Humidity: {weather[1]}%, Chance of rain: {weather[2]}%."
            if weather[2] > 0:
                message += "\nRemember to bring waterproof gear."
        else:
            message += "\nWeather data unavailable."
        context.bot.send_message(chat_id=user_settings["chat_id"], text=message)

    def job_departure_work():
        departure_ts = int(datetime.now().timestamp())
        minutes = get_travel_time(user_settings["work_address"], user_settings["home_address"], departure_ts)
        arrival_time = (datetime.now() + timedelta(minutes=minutes)).strftime("%H:%M") if minutes else "unknown"
        weather = get_weather(user_settings["work_lat"], user_settings["work_lon"])
        message = f"Time to go! Expect a {minutes} minute drive to Home. Arrival is expected at {arrival_time}.\n"
        if weather[0] is not None:
            message += f"Weather: {weather[0]}°C, {weather[3]}, Humidity: {weather[1]}%, Chance of rain: {weather[2]}%."
            if weather[2] > 0:
                message += " Tip: Wear waterproof clothing."
        else:
            message += "Weather data unavailable."
        context.bot.send_message(chat_id=user_settings["chat_id"], text=message)

    def job_pre_departure_work():
        departure_ts = int((datetime.now() + timedelta(minutes=30)).timestamp())
        minutes = get_travel_time(user_settings["work_address"], user_settings["home_address"], departure_ts)
        weather = get_weather(user_settings["work_lat"], user_settings["work_lon"])
        message = f"Plan to leave by {user_settings['depart_work']} to Home.\nPredicted travel time: {minutes} minutes.\n"
        if weather[0] is not None:
            message += f"Weather: {weather[0]}°C, {weather[3]}, Humidity: {weather[1]}%, Chance of rain: {weather[2]}%."
            if weather[2] > 0:
                message += "\nRemember your waterproof gear."
        else:
            message += "\nWeather data unavailable."
        context.bot.send_message(chat_id=user_settings["chat_id"], text=message)

    # Schedule jobs if settings are available
    if "depart_home" in user_settings:
        add_job(user_settings['depart_home'], job_departure_home, 'home_departure')
        pre_home_time = (datetime.strptime(user_settings['depart_home'], "%H:%M") - timedelta(minutes=30)).time().strftime("%H:%M")
        add_job(pre_home_time, job_pre_departure_home, 'home_pre_departure')

    if "depart_work" in user_settings:
        add_job(user_settings['depart_work'], job_departure_work, 'work_departure')
        pre_work_time = (datetime.strptime(user_settings['depart_work'], "%H:%M") - timedelta(minutes=30)).time().strftime("%H:%M")
        add_job(pre_work_time, job_pre_departure_work, 'work_pre_departure')

    scheduler.start()

# ------------------ Telegram Bot Handlers ------------------
def start(update: Update, context: CallbackContext) -> int:
    update.message.reply_text(
        "Welcome! Please share your **Home** location using Telegram’s location sharing feature.",
        parse_mode="Markdown")
    return HOME

def home_location(update: Update, context: CallbackContext) -> int:
    if update.message.location:
        home_loc = update.message.location
        user_settings["home_lat"] = home_loc.latitude
        user_settings["home_lon"] = home_loc.longitude
        user_settings["home_address"] = f"{home_loc.latitude},{home_loc.longitude}"
        user_settings["chat_id"] = update.message.chat_id
        update.message.reply_text("Home location saved. Now, please share your **Work** location.", parse_mode="Markdown")
        save_settings()
        return WORK
    else:
        update.message.reply_text("Please share your location using the Telegram location feature.")
        return HOME

def work_location(update: Update, context: CallbackContext) -> int:
    if update.message.location:
        work_loc = update.message.location
        user_settings["work_lat"] = work_loc.latitude
        user_settings["work_lon"] = work_loc.longitude
        user_settings["work_address"] = f"{work_loc.latitude},{work_loc.longitude}"
        update.message.reply_text("Work location saved. Now please send your departure time from Home (HH:MM).")
        save_settings()
        return DEPART_HOME
    else:
        update.message.reply_text("Please share your location.")
        return WORK

def depart_home_time(update: Update, context: CallbackContext) -> int:
    time_text = update.message.text
    try:
        datetime.strptime(time_text, "%H:%M")
        user_settings["depart_home"] = time_text
        update.message.reply_text("Home departure time saved. Now please send your departure time from Work (HH:MM).")
        save_settings()
        return DEPART_WORK
    except ValueError:
        update.message.reply_text("Invalid time format. Please send time as HH:MM (e.g., 08:30).")
        return DEPART_HOME

def depart_work_time(update: Update, context: CallbackContext) -> int:
    time_text = update.message.text
    try:
        datetime.strptime(time_text, "%H:%M")
        user_settings["depart_work"] = time_text
        update.message.reply_text("Work departure time saved! Your notifications will be scheduled automatically.")
        save_settings()
        schedule_notifications(context)
        return ConversationHandler.END
    except ValueError:
        update.message.reply_text("Invalid time format. Please send time as HH:MM (e.g., 18:00).")
        return DEPART_WORK

def cancel(update: Update, context: CallbackContext) -> int:
    update.message.reply_text("Setup cancelled.")
    return ConversationHandler.END

# ------------------ Main Function ------------------
def main():
    load_settings()
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            HOME: [MessageHandler(Filters.location, home_location)],
            WORK: [MessageHandler(Filters.location, work_location)],
            DEPART_HOME: [MessageHandler(Filters.text & ~Filters.command, depart_home_time)],
            DEPART_WORK: [MessageHandler(Filters.text & ~Filters.command, depart_work_time)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    dp.add_handler(conv_handler)
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
