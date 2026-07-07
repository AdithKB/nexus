import os, time
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.environ["GEMINI_API_KEY"])

video_path = "/tmp/audit_video.mp4"
if not os.path.exists(video_path):
    print(f"Error: {video_path} not found")
    exit(1)

print("Uploading video to Gemini for actual verification...")
video_file = genai.upload_file(path=video_path)

while video_file.state.name == "PROCESSING":
    time.sleep(2)
    video_file = genai.get_file(video_file.name)

notes_content = """
1. AI Engineering Book: RAG, chunking, reranking, prompt engineering, multi-modal, infrastructure, evaluation.
2. Designing Data-Intensive Applications: Data storage (SQL, NoSQL, vector DBs), consistency, batch vs streaming, failure modes.
3. Designing Machine Learning Systems: production success (13% vs 87%), data storage, deployment, monitoring.
4. Machine Learning System Design Interview: structured thinking, pattern recognition, trade-offs, scalability, cost estimation.
5. Machine Learning with PyTorch and Scikit-Learn: PyTorch foundation, practical ML development.
6. Mathematics for Machine Learning: understanding models (gradient descent) vs just using libraries.
7. Natural Language Processing with Transformers: mechanics of LLMs, attention, tokenization.
8. Practical Statistics for Data Scientists: A/B testing, data drift, decision making.
9. Python Natural Language Processing Cookbook: TF-IDF, BM25, classical RAG debugging.
"""

prompt = f"""
You are an expert auditor. Watch the attached video (9 book recommendations).
Check these 9 notes for veracity:
{notes_content}

TASKS:
1. Does the video actually mention all 9 of these books?
2. Are the specific details (like '87% failure rate' or 'BM25') actually in the video or did the model guess them?
3. Check the note list above: is it in alphabetical order?

Output a Veracity Score (0-10) and a final verdict.
"""

model = genai.GenerativeModel(model_name="gemini-2.5-flash")
response = model.generate_content([video_file, prompt])
print("\n--- ACTUAL AUDIT RESULTS ---")
print(response.text)

genai.delete_file(video_file.name)
