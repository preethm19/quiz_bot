import re
import asyncio
import random
import google.generativeai as genai
from telegram import Update, Poll
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    JobQueue
)
from datetime import datetime, timedelta
import pytz
import logging

# ====== Configuration ======
TELEGRAM_TOKEN = '8166082829:AAG-XMCwvT-HSB_foI3Op1kDmm8J99OKJV4'
GEMINI_API_KEY = 'AIzaSyDMfwUMyqS6U0OPN8ijHhCAHs_C6amqPRA'

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_API_KEY)
last_explanation = {}

# ====== Conversation History with Context Memory ======
class ConversationHistory:
    def __init__(self):
        self.history = {}  # {chat_id: [{"user": text, "bot": response}, ...]}
        self.reminders = []  # [{"chat_id": id, "time": datetime, "task": task, "topic": topic}, ...]
        self.last_quiz = {}  # {chat_id: {"poll_id": id, "answers": [correct_indices]}}

    def add_message(self, chat_id: int, user_text: str, bot_response: str):
        if chat_id not in self.history:
            self.history[chat_id] = []
        self.history[chat_id].append({"user": user_text, "bot": bot_response})
        if len(self.history[chat_id]) > 20:
            self.history[chat_id] = self.history[chat_id][-20:]

    def get_history(self, chat_id: int):
        return self.history.get(chat_id, [])

    def add_reminder(self, chat_id: int, time: datetime, task: str, topic: str = None):
        self.reminders.append({"chat_id": chat_id, "time": time, "task": task, "topic": topic})

    def store_quiz(self, chat_id: int, poll_id: int, correct_indices: list):
        self.last_quiz[chat_id] = {"poll_id": poll_id, "answers": correct_indices}

    def get_last_quiz(self, chat_id: int):
        return self.last_quiz.get(chat_id, None)

    def get_last_quiz_topic(self, chat_id: int):
        history = self.get_history(chat_id)
        for entry in reversed(history):
            if "Generated" in entry["bot"] and "quiz" in entry["bot"]:
                topic_match = re.search(r"quiz.*?\s+on\s+(.+?)(?:\s|$)", entry["bot"], re.IGNORECASE)
                if topic_match:
                    return topic_match.group(1).capitalize()
            user_text = entry["user"].lower()
            quiz_match = re.search(r"quiz.*?\s+(?:on|about)?\s*(.+?)(?:\s|$)", user_text, re.IGNORECASE)
            if quiz_match:
                return quiz_match.group(1).capitalize()
        return None

# Initialize history
conversation_history = ConversationHistory()

# ====== Resolve Ambiguous Topics ======
async def resolve_topic(chat_id: int, topic: str) -> str:
    if not topic or topic.strip() == "":
        last_topic = conversation_history.get_last_quiz_topic(chat_id)
        if last_topic:
            return last_topic
        return "general knowledge"
    
    topic = topic.lower().strip()
    if topic in ["them", "it", "those", "that", "last topic", "last topic i asked for"]:
        last_topic = conversation_history.get_last_quiz_topic(chat_id)
        if last_topic:
            return last_topic
        history = conversation_history.get_history(chat_id)[-5:]
        for entry in reversed(history):
            user_text = entry["user"].lower()
            words = user_text.split()
            for word in words:
                if word not in ["do", "you", "know", "about", "what", "is", "a", "the", "on", "quiz", "send", "give"]:
                    return word.capitalize()
    return topic.capitalize()

# ====== Parse Reminder Time ======
def parse_reminder_time(text: str, base_time: datetime = None) -> datetime:
    tz = pytz.timezone('Asia/Kolkata')  # IST for Preetham
    if not base_time:
        base_time = datetime.now(tz)
    
    text = text.lower().strip() if text else ""
    
    # Match "at HH:MM AM/PM [date]"
    time_match = re.search(r"at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*(\d{1,2}(?:st|nd|rd|th)?\s*(?:january|february|march|april|may|june|july|august|september|october|november|december)?\s*\d{4}?))?", text, re.IGNORECASE)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        period = time_match.group(3)
        date_str = time_match.group(4)
        
        if period == "pm" and hour < 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0
            
        if date_str:
            date_match = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s*(\w+)?\s*(\d{4})?", date_str, re.IGNORECASE)
            day = int(date_match.group(1))
            month_name = date_match.group(2).lower() if date_match.group(2) else None
            year = int(date_match.group(3)) if date_match.group(3) else base_time.year
            month_map = {
                "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
                "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12
            }
            month = month_map.get(month_name, base_time.month)
            if month < base_time.month and not month_name and year == base_time.year:
                year += 1
            reminder_time = tz.localize(datetime(year, month, day, hour, minute))
            return reminder_time
        
        reminder_time = base_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reminder_time < base_time:
            reminder_time += timedelta(days=1)
        return reminder_time
    
    # Match "in X hours/minutes"
    in_match = re.search(r"in\s+(\d+)\s+(hour|hours|minute|minutes)", text, re.IGNORECASE)
    if in_match:
        amount = int(in_match.group(1))
        unit = in_match.group(2).lower()
        if "hour" in unit:
            return base_time + timedelta(hours=amount)
        elif "minute" in unit:
            return base_time + timedelta(minutes=amount)
    
    return base_time + timedelta(hours=1)

# ====== Send Reminder Notification ======
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    task = job.data["task"]
    topic = job.data.get("topic")
    message = f"⏰ Reminder: Time to {task}!"
    if topic:
        message += f" Want a quiz on {topic} now?"
    try:
        await context.bot.send_message(chat_id=chat_id, text=message)
        logger.info(f"Sent reminder to {chat_id}: {task} at {datetime.now(pytz.timezone('Asia/Kolkata'))}")
        # Remove from reminders
        conversation_history.reminders[:] = [
            r for r in conversation_history.reminders
            if not (r["chat_id"] == chat_id and r["task"] == task and abs((r["time"] - datetime.now(pytz.timezone('Asia/Kolkata'))).total_seconds()) < 60)
        ]
    except Exception as e:
        logger.error(f"Reminder send error for chat {chat_id}: {e}")

# ====== Reminder Scheduler (Fallback) ======
async def reminder_scheduler(context: ContextTypes.DEFAULT_TYPE):
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz)
    for reminder in conversation_history.reminders[:]:
        if now >= reminder["time"]:
            try:
                message = f"⏰ Reminder: Time to {reminder['task']}!"
                if reminder["topic"]:
                    message += f" Want a quiz on {reminder['topic']} now?"
                await context.bot.send_message(chat_id=reminder["chat_id"], text=message)
                logger.info(f"Fallback sent reminder to {reminder['chat_id']}: {reminder['task']} at {now}")
                conversation_history.reminders.remove(reminder)
            except Exception as e:
                logger.error(f"Fallback reminder error for chat {reminder['chat_id']}: {e}")
        elif reminder["time"] < now - timedelta(minutes=5):
            logger.info(f"Removing stale reminder: {reminder['task']} at {reminder['time']}")
            conversation_history.reminders.remove(reminder)

# ====== /start Handler ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    response = (
        "🧠 Hello! I'm your QuizBot Assistant\n\n"
        "• Ask for quizzes like: 'send 3 quizzes on biology' (always in poll format)\n"
        "• Reply to any message to continue that topic\n"
        "• Set reminders, e.g., 'remind me to drink water in 2 minutes'\n"
        "• Ask me anything, like 'Do you know Beluga?'"
    )
    await update.message.reply_text(response)
    conversation_history.add_message(update.message.chat.id, "/start", response)

# ====== Message Handler ======
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    lower_text = text.lower()
    chat_id = update.message.chat.id
    bot_response = None

    # Handle replies to any message
    if update.message.reply_to_message:
        replied_message = update.message.reply_to_message.text
        history = conversation_history.get_history(chat_id)
        replied_context = next(
            (entry for entry in reversed(history) if entry["bot"] == replied_message),
            None
        )
        if replied_context:
            # Reply to history message
            if any(x in lower_text for x in ["quiz", "question"]):
                topic = re.search(r"(?:on|about|for|related to)\s*(.+)", replied_context["user"], re.IGNORECASE)
                topic = topic.group(1).strip() if topic else await resolve_topic(chat_id, "")
                await process_quizzes(update, topic, 1)
                bot_response = f"Quiz on {topic} as you replied!"
            elif any(x in lower_text for x in ["why", "explain", "reason"]):
                if chat_id in last_explanation:
                    bot_response = f"🔍 Explanation:\n{last_explanation[chat_id]}"
                    await update.message.reply_text(bot_response)
                else:
                    bot_response = "No quiz explanation available!"
                    await update.message.reply_text(bot_response)
            else:
                bot_response = await generate_reply_to_context(
                    update,
                    text,
                    replied_context["user"],
                    replied_context["bot"]
                )
                await update.message.reply_text(bot_response)
        else:
            # Reply to arbitrary message
            bot_response = await generate_reply_to_context(update, text, replied_message, replied_message)
            await update.message.reply_text(bot_response)

    # Handle new messages
    else:
        # Reminder request
        reminder_match = re.search(r"remind\s+me\s+to\s+(.+?)(?:\s+(at|in)\s+(.+))?$", lower_text, re.IGNORECASE)
        if reminder_match:
            task = reminder_match.group(1).strip()
            time_str = reminder_match.group(3).strip() if reminder_match.group(2) else None
            topic = None
            if "quiz" in task:
                topic_match = re.search(r"quiz\s+(?:on|about)\s+(.+)", task, re.IGNORECASE)
                topic = topic_match.group(1).strip() if topic_match else await resolve_topic(chat_id, "")
            reminder_time = parse_reminder_time(time_str)
            delay = (reminder_time - datetime.now(pytz.timezone('Asia/Kolkata'))).total_seconds()
            if delay < 0:
                bot_response = "That time is in the past! Please pick a future time."
            elif delay > 86400:  # Limit to 1 day
                bot_response = "Sorry, reminders can't be set more than 24 hours ahead!"
            else:
                conversation_history.add_reminder(chat_id, reminder_time, task, topic)
                context.job_queue.run_once(
                    send_reminder,
                    when=delay,
                    data={"chat_id": chat_id, "task": task, "topic": topic},
                    name=f"reminder_{chat_id}_{task}_{reminder_time.timestamp()}"
                )
                bot_response = f"Got it! I'll remind you to {task} at {reminder_time.strftime('%I:%M %p %d %b')}."
                logger.info(f"Scheduled reminder for {chat_id}: {task} at {reminder_time}")
            await update.message.reply_text(bot_response)

        # Quiz request
        quiz_match = re.search(
            r"(?:give|send|make|create|want|need|quiz|question)?(?:\s*me)?\s*(\d+)?\s*(?:quiz(?:zes)?|question(?:s)?)\s*(?:on|about|for|related\s+to)?\s*(.+)?",
            lower_text,
            re.IGNORECASE
        )
        if quiz_match and "remind" not in lower_text:
            num_quizzes = int(quiz_match.group(1)) if quiz_match.group(1) else 1
            topic = quiz_match.group(2).strip() if quiz_match.group(2) else ""
            topic = await resolve_topic(chat_id, topic)
            await process_quizzes(update, topic, num_quizzes)
            bot_response = f"Sent {num_quizzes} quiz{'zes' if num_quizzes > 1 else ''} on {topic} as polls!"

        # Check for quiz answers (e.g., "3 4")
        elif re.match(r"^\d+(\s+\d+)*$", lower_text):
            last_quiz = conversation_history.get_last_quiz(chat_id)
            if last_quiz:
                answers = [int(x) - 1 for x in lower_text.split()]
                correct = all(a in last_quiz["answers"] for a in answers)
                bot_response = "Correct! 🎉" if correct else "Not quite, try again or type 'why' for explanations."
                await update.message.reply_text(bot_response)
            else:
                bot_response = "No recent quiz to answer. Want to start one?"
                await update.message.reply_text(bot_response)

        # Explanation request
        elif any(x in lower_text for x in ["why", "explain", "reason"]):
            if chat_id in last_explanation:
                bot_response = f"🔍 Explanation:\n{last_explanation[chat_id]}"
                await update.message.reply_text(bot_response)
            else:
                bot_response = "No quiz explanation available yet!"
                await update.message.reply_text(bot_response)

        # General conversation
        else:
            bot_response = await handle_general_chat(update, text)
            await update.message.reply_text(bot_response)

    # Store conversation
    if bot_response and not quiz_match:
        conversation_history.add_message(chat_id, text, bot_response)

# ====== Reply to Context ======
async def generate_reply_to_context(update: Update, text: str, original_user_text: str, original_bot_response: str):
    try:
        model = genai.GenerativeModel('models/gemini-1.5-flash')
        history = conversation_history.get_history(update.message.chat.id)[-5:]
        context = "\n".join([f"User: {h['user']}\nBot: {h['bot']}" for h in history])
        prompt = (
            f"You're a friendly AI assistant with memory. Recent conversation:\n{context}\n"
            f"The user sent '{text}', replying to a message where they said '{original_user_text}' "
            f"and you responded '{original_bot_response}'. Respond naturally in 1-3 sentences, "
            f"using the full conversation history to stay relevant."
        )
        response = model.generate_content(prompt)
        reply = response.text.strip() or "Thanks for replying! What's next?"
        return reply
    except Exception as e:
        logger.error(f"Reply generation error: {e}")
        return "Oops, something went wrong!"

# ====== Quiz Processing ======
async def process_quizzes(update: Update, topic: str, quantity: int):
    if not topic:
        topic = "general knowledge"
    
    await update.message.reply_text(f"📚 Sending {quantity} quiz{'zes' if quantity > 1 else ''} about {topic} as polls...")
    quizzes, explanations = generate_quiz(topic, quantity)
    
    if not quizzes:
        await update.message.reply_text("Failed to generate quizzes. Please try another topic!")
        return
    
    correct_indices = []
    for i, quiz in enumerate(quizzes):
        poll_id = await send_quiz_poll(update, quiz, update.message.chat.id)
        if poll_id:
            answer = quiz.split("Answer:")[1].split("Explanation:")[0].strip().upper()
            options = quiz.split("Options:")[1].split("Answer:")[0].strip().split("\n")
            correct_index = next(i for i, opt in enumerate(options) if opt.startswith(answer + ")"))
            correct_indices.append(correct_index)
            explanations[i] = f"Quiz {i+1} Explanation:\n{explanations[i]}"
        await asyncio.sleep(1)
    
    if explanations:
        last_explanation[update.message.chat.id] = "\n\n".join(explanations)
        conversation_history.store_quiz(update.message.chat.id, poll_id, correct_indices)

# ====== General Conversation ======
async def handle_general_chat(update: Update, text: str):
    try:
        model = genai.GenerativeModel('models/gemini-1.5-flash')
        history = conversation_history.get_history(update.message.chat.id)[-5:]
        context = "\n".join([f"User: {h['user']}\nBot: {h['bot']}" for h in history])
        prompt = (
            f"You're a friendly AI assistant with memory. Recent conversation:\n{context}\n"
            f"Respond to the user's latest message in 1-3 sentences, using the history to stay relevant:\n{text}"
        )
        response = model.generate_content(prompt)
        reply = response.text.strip() or "Hmm, not sure what to say. What's up?"
        return reply
    except Exception as e:
        logger.error(f"Chat error: {e}")
        return "Oops, something went wrong."

# ====== Quiz Generation ======
def generate_quiz(topic: str, quantity: int):
    try:
        model = genai.GenerativeModel('models/gemini-1.5-flash')
        prompt = f"""Create exactly {quantity} distinct multiple-choice quiz questions about {topic}. Each question must:
- Be unique and cover different aspects of {topic}
- Have one clear question
- Have exactly 4 options labeled A, B, C, D
- Specify the correct answer as a single letter (A, B, C, or D)
- Include a brief explanation (1-2 sentences)
- Follow the exact format below with no deviations

Format for each quiz:
Quiz [number]:
Question: [question text]
Options:
A) [option 1]
B) [option 2]
C) [option 3]
D) [option 4]
Answer: [correct letter]
Explanation: [1-2 sentence explanation]

Example:
Quiz 1:
Question: What is the capital of France?
Options:
A) Florida
B) Paris
C) Texas
D) Narnia
Answer: B
Explanation: Paris is the capital city of France, known for its cultural landmarks.

Ensure each quiz is separated by 'Quiz [number]:' and all {quantity} quizzes are included."""
        
        for attempt in range(3):
            response = model.generate_content(prompt)
            quiz_text = response.text.strip()
            quizzes = []
            explanations = []
            
            quiz_blocks = re.split(r'Quiz \d+:', quiz_text)[1:] if 'Quiz ' in quiz_text else [quiz_text]
            
            for block in quiz_blocks:
                try:
                    block = block.strip()
                    if not block:
                        continue
                    
                    question_match = re.search(r'Question:\s*(.*?)\s*Options:', block, re.DOTALL)
                    options_match = re.search(r'Options:\s*(.*?)\s*Answer:', block, re.DOTALL)
                    answer_match = re.search(r'Answer:\s*([A-D])', block)
                    explanation_match = re.search(r'Explanation:\s*(.*?)$', block, re.DOTALL)
                    
                    if not all([question_match, options_match, answer_match, explanation_match]):
                        logger.warning(f"Skipping malformed quiz block: {block[:100]}...")
                        continue
                    
                    question = question_match.group(1).strip()
                    options_text = options_match.group(1).strip()
                    answer = answer_match.group(1).strip().upper()
                    explanation = explanation_match.group(1).strip()
                    
                    options = [opt.strip() for opt in options_text.split('\n') if opt.strip()]
                    if len(options) != 4:
                        logger.warning(f"Invalid number of options in block: {block[:100]}...")
                        continue
                    
                    option_pairs = [(chr(65+i), opt[3:]) for i, opt in enumerate(options)]
                    correct_text = next(opt[1] for opt in option_pairs if opt[0] == answer)
                    
                    random.shuffle(option_pairs)
                    
                    shuffled_options = [f"{chr(65+i)}) {opt[1]}" for i, opt in enumerate(option_pairs)]
                    new_answer = next(chr(65+i) for i, opt in enumerate(option_pairs) if opt[1] == correct_text)
                    
                    shuffled_quiz = (
                        f"Question: {question}\n"
                        f"Options:\n" + "\n".join(shuffled_options) + "\n"
                        f"Answer: {new_answer}\n"
                        f"Explanation: {explanation}"
                    )
                    
                    quizzes.append(shuffled_quiz)
                    explanations.append(explanation)
                
                except Exception as e:
                    logger.error(f"Error parsing quiz block: {e}, Block: {block[:100]}...")
                    continue
            
            if len(quizzes) >= quantity:
                return quizzes[:quantity], explanations[:quantity]
            
            logger.warning(f"Attempt {attempt+1} yielded {len(quizzes)} quizzes, needed {quantity}. Retrying...")
        
        logger.error(f"Failed to generate {quantity} quizzes after 3 attempts. Raw response: {quiz_text[:500]}...")
        return [], []
    
    except Exception as e:
        logger.error(f"Quiz generation error: {e}")
        return [], []

# ====== Poll Sending ======
async def send_quiz_poll(update: Update, quiz_text: str, chat_id) -> int:
    try:
        question = quiz_text.split("Question:")[1].split("Options:")[0].strip()
        options_text = quiz_text.split("Options:")[1].split("Answer:")[0].strip()
        options = [opt.strip() for opt in options_text.split("\n") if opt.strip()]
        answer = quiz_text.split("Answer:")[1].split("Explanation:")[0].strip().upper()
        
        poll_options = [opt[3:].strip() for opt in options]
        correct_index = next((i for i, opt in enumerate(options) if opt.startswith(answer + ")")), 0)
        
        poll = await update.message.reply_poll(
            question=question,
            options=poll_options,
            type=Poll.QUIZ,
            correct_option_id=correct_index,
            is_anonymous=False,
            explanation="Type 'why' for explanation"
        )
        return poll.poll.id
    except Exception as e:
        logger.error(f"Poll error: {e}")
        await update.message.reply_text("Failed to create quiz poll. Please try another topic!")
        return None

# ====== Main ======
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.job_queue.run_repeating(reminder_scheduler, interval=30, first=0)
    logger.info("🤖 QuizBot is running...")
    app.run_polling()

if __name__ == '__main__':
    main()