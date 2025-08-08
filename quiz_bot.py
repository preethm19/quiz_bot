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
    ContextTypes
)

# ====== Configuration ======
TELEGRAM_TOKEN = '8166082829:AAG-XMCwvT-HSB_foI3Op1kDmm8J99OKJV4'
GEMINI_API_KEY = 'AIzaSyALMLpVIuw3O1LopgwI23ZmV2GqOYHXALQ'

genai.configure(api_key=GEMINI_API_KEY)
last_explanation = {}

# ====== /start Handler ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧠 Hello! I'm your QuizBot Assistant\n\n"
        "• Ask for quizzes like: 'send 3 quizzes on biology' or 'give me a quiz about coding'\n"
        "• After a quiz, say 'why' for an explanation\n"
        "• Or just chat with me about anything!"
    )

# ====== Message Handler ======
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    lower_text = text.lower()
    chat_id = update.message.chat.id

    # Flexible quiz request detection
    quiz_match = re.search(r"(?:give|send|make|create|want|need|quiz|question)(?:\s*me)?\s*(\d+)?\s*(?:quiz(?:zes)?|question(?:s)?)(?:\s*(?:on|about|for|related to)\s*(.+))?", lower_text, re.IGNORECASE)
    
    if quiz_match:
        num_quizzes = int(quiz_match.group(1)) if quiz_match.group(1) else 1
        topic = quiz_match.group(2).strip() if quiz_match.group(2) else "general knowledge"
        await process_quizzes(update, topic, num_quizzes)
    
    # Explanation request
    elif any(x in lower_text for x in ["why", "explain", "reason"]):
        if chat_id in last_explanation:
            await update.message.reply_text(f"🔍 Explanation:\n{last_explanation[chat_id]}")
        else:
            await update.message.reply_text("No quiz explanation available yet!")
    
    # General conversation
    else:
        await handle_general_chat(update, text)

# ====== Quiz Processing ======
async def process_quizzes(update: Update, topic: str, quantity: int):
    if not topic:
        topic = "general knowledge"
    
    await update.message.reply_text(f"📚 Preparing {quantity} quiz{'zes' if quantity>1 else ''} about {topic}...")
    quizzes, explanations = generate_quiz(topic, quantity)
    
    if not quizzes:
        await update.message.reply_text("Failed to generate quizzes. Please try another topic!")
        return
    
    for i, quiz in enumerate(quizzes):
        if await send_quiz_poll(update, quiz, update.message.chat.id):
            explanations[i] = f"Quiz {i+1} Explanation:\n{explanations[i]}"
        await asyncio.sleep(1)
    
    if explanations:
        last_explanation[update.message.chat.id] = "\n\n".join(explanations)

# ====== General Conversation ======
async def handle_general_chat(update: Update, text: str):
    try:
        model = genai.GenerativeModel('models/gemini-1.5-flash')  # Upgraded to a more conversational model
        response = model.generate_content(
            f"You're a friendly and knowledgeable AI assistant. Respond helpfully and naturally to this message in 1-3 sentences, adapting to the user's tone and context:\n{text}"
        )
        reply = response.text.strip()
        if not reply:
            reply = "Hmm, I'm not sure what to say to that. Could you give me a bit more to work with?"
        await update.message.reply_text(reply)
    except Exception as e:
        print(f"Chat error: {e}")
        await update.message.reply_text("Oops, something went wrong. Could you try again?")

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
                        print(f"Skipping malformed quiz block: {block[:100]}...")
                        continue
                    
                    question = question_match.group(1).strip()
                    options_text = options_match.group(1).strip()
                    answer = answer_match.group(1).strip().upper()
                    explanation = explanation_match.group(1).strip()
                    
                    options = [opt.strip() for opt in options_text.split('\n') if opt.strip()]
                    if len(options) != 4:
                        print(f"Invalid number of options in block: {block[:100]}...")
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
                    print(f"Error parsing quiz block: {e}, Block: {block[:100]}...")
                    continue
            
            if len(quizzes) >= quantity:
                return quizzes[:quantity], explanations[:quantity]
            
            print(f"Attempt {attempt+1} yielded {len(quizzes)} quizzes, needed {quantity}. Retrying...")
        
        print(f"Failed to generate {quantity} quizzes after 3 attempts. Raw response: {quiz_text[:500]}...")
        return [], []
    
    except Exception as e:
        print(f"Quiz generation error: {e}")
        return [], []

# ====== Poll Sending ======
async def send_quiz_poll(update: Update, quiz_text: str, chat_id) -> bool:
    try:
        question = quiz_text.split("Question:")[1].split("Options:")[0].strip()
        options_text = quiz_text.split("Options:")[1].split("Answer:")[0].strip()
        options = [opt.strip() for opt in options_text.split("\n") if opt.strip()]
        answer = quiz_text.split("Answer:")[1].split("Explanation:")[0].strip().upper()
        
        poll_options = [opt[3:].strip() for opt in options]
        correct_index = next((i for i, opt in enumerate(options) if opt.startswith(answer + ")")), 0)
        
        await update.message.reply_poll(
            question=question,
            options=poll_options,
            type=Poll.QUIZ,
            correct_option_id=correct_index,
            is_anonymous=False,
            explanation="Type 'why' for explanation"
        )
        return True
    except Exception as e:
        print(f"Poll error: {e}")
        await update.message.reply_text("Failed to create quiz. Please try another topic!")
        return False

# ====== Main ======
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 QuizBot is running...")
    app.run_polling()

if __name__ == '__main__':
    main()
