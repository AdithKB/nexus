# Nexus: A Local-First, AI-Powered Second Brain

**"I watched an incredible 40-minute lecture on YouTube, sent the link to a friend, and then... I forgot everything. I realized that saving a link is not the same as capturing knowledge. That insight was lost in a graveyard of 'Watch Later' lists."**

---

## 📖 The Story: Escaping "Information Rot"
We are drowning in more high-quality information than any generation in history, yet we remember less of it. 

Every day, I would find a life-changing insight in a YouTube lecture, a technical deep-dive on Instagram, or a complex research PDF. My solution was to "save the link." But **links are not knowledge.** They are just digital addresses to rooms we never visit again. I was suffering from **"Information Rot"**—the slow decay of valuable ideas that were never processed or connected.

I needed a system that could **capture** these insights instantly while keeping my private thoughts and notes **100% local.** I wanted to build a "Second Brain" in **Obsidian** where my Mac remained the only "Source of Truth."

**That is why I created Nexus.**

---

## 🛠️ The Architecture: "The Cloud-Inbox"
Nexus uses a unique **"Local-First"** architecture. It treats the cloud as a high-powered, zero-cost "Inbox" while keeping your private data on your own machine.

### 1. The Cloud: A Private Post Office
When you find a link on your iPhone, Nexus wakes up in **GCP Cloud Run**. It "watches" the video, "reads" the transcript, and synthesizes **Atomic Notes**. It stores these notes in a temporary "GCP Inbox" (Cloud Storage) until your Mac pulls them in.

### 2. The Index: Privacy-Aware Linking
To help the AI link new notes to your existing knowledge, your Mac "pushes" only your **Note Titles and Aliases** to a **GCP Firestore** database. The cloud never sees the content of your notes—only a "Map" of your vault.

### 3. The Mac: The RAG Engine
Because your notes stay local, you can use high-powered CLI tools like **Gemini CLI** or **Claude Code** to talk to your entire vault. 
*   **The Power:** You can ask: *"Based on my last 6 months of research, what are the recurring patterns in my thinking?"* 
*   **The Result:** You get instant, private answers grounded in your own unique knowledge base.

---

## 🏗️ The Engineering: Mastering Serverless
To build this for **$0/month**, I had to solve major technical hurdles:

*   **Scale-to-Zero:** The system is built on **GCP Cloud Run**. It costs $0 until you send a URL, wakes up in seconds, and disappears when done.
*   **Persistent Index:** Since serverless containers forget everything, I moved the "Map" of my vault to **Firestore**, ensuring the AI always knows how to link new notes.
*   **Bypassing Blocks:** I implemented a specialized environment using the **Deno JS runtime** and **PO Tokens** to bypass YouTube's data center blocks, ensuring 100% reliable capture.

---

## 🚀 The Result: A Universal Knowledge Pipe
Today, Nexus is no longer just a "Video Capture" tool. It has become my **Universal Inbox** for all digital information:
*   **YouTube/Instagram:** Extracts the core argument from video and audio.
*   **Web Articles:** Uses Jina Reader to "distill" long-form articles into atomic insights.
*   **GitHub Repos:** Scrapes `README.md` files to understand the purpose and tech stack of new libraries.
*   **Research Papers:** Parses PDFs to extract methodology, results, and key takeaways.

Because of the **Semantic Search** I built using Firestore's vector queries, I frequently experience "Aha!" moments where the system reminds me of a connection I would have never made manually. My vault is no longer a graveyard of links; it is a **Living Knowledge Base** that grows more valuable with every capture.

---
**Tech Stack:** Python, FastAPI, Docker, GCP (Cloud Run, Firestore, GCS), Gemini AI, yt-dlp, ffmpeg.
**Obsidian Workflow:** Zettelkasten, Atomic Notes, Local RAG, MOCs.
