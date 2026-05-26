# Online_Grooming
📌 Online Grooming Detection System (AI + Sandbox Safety Mode)
🚀 Overview

This project is a real-time AI-based child online safety system that detects grooming behavior in chat conversations and dynamically restricts interaction using a sandbox escalation system.

It combines:

🧠 LLM-based risk scoring (Groq Llama 3.3)
🔍 Regex + keyword safety fallback
⚡ Real-time WebSocket chat (Flask-SocketIO)
🚨 Risk aggregation engine
🛡️ Sandbox containment mode for high-risk scenarios
📧 Alerting + evidence logging system
🧠 Core Idea

Every message is scored between:

0.0 → safe conversation
1.0 → highly dangerous grooming behavior

The system continuously accumulates risk and triggers SANDBOX MODE when threshold is crossed.

🏗️ System Architecture
User Message
     ↓
Flask-SocketIO Server
     ↓
LLM Risk Scoring (Groq Llama 3.3)
     ↓
Regex + Keyword Fallback
     ↓
Risk Aggregation Engine
     ↓
IF risk < threshold → normal chat
IF risk ≥ threshold → SANDBOX MODE
🚨 SANDBOX MODE (KEY FEATURE)
🔴 What is Sandbox Mode?

Sandbox Mode is a containment safety state activated when the system detects high cumulative grooming risk.

It prevents normal conversation flow and restricts interaction.

⚙️ How it triggers

From code:

SANDBOX_THRESHOLD = 1.0
room_risk ≥ 1.0 → SANDBOX ACTIVATED

When triggered:

room_sandbox = True
Evidence is saved (save_evidence())
UI gets "sandbox_activated" event
🔁 What happens inside Sandbox Mode

Once active:

1. Chat behavior changes
Messages still processed but restricted
Responses may be limited or controlled
Risk continues to be monitored
2. Wind-down mechanism
WIND_DOWN_AFTER = 2

After a few sandbox messages:

System starts wind-down phase
Gradually reduces interaction intensity
3. Message tracking inside sandbox
sandbox_msg_count increases per message
Used to control escalation logic
Prevents immediate exit from sandbox state
4. System logs

Example logs:

[SANDBOX ACTIVATED] risk=1.02
[SANDBOX→child] message restricted
[SANDBOX→predator] message filtered
🧠 AI Risk Scoring System

Uses Groq LLM with strict prompt:

Score meanings:
0.0 → normal chat (greetings, hobbies, school talk)
0.1–0.3 → mild personal questions
0.3–0.5 → suspicious behavior
0.7+ → grooming behavior
🔍 Regex Safety Layer (Backup)

If LLM fails, system uses regex detection:

Detects:

secrecy requests ("don’t tell your parents")
isolation ("are you alone?")
explicit content ("send nudes")
grooming patterns ("come meet me")
⚡ Real-Time Communication

Built using:

Flask
Flask-SocketIO
Gevent async server

Features:

Live chat rooms (safeguard-room)
Role-based users (child / predator simulation)
Instant risk evaluation per message
📧 Alert System

Triggers alerts when:

High-risk messages detected
Sandbox is activated
Grooming patterns identified

Uses:

SMTP email
Resend API (optional)
📊 Evidence Logging

All sessions are saved as:

evidence_YYYYMMDD_HHMMSS.json

Stores:

Messages
Risk scores
Sandbox events
Detection logs
🧰 Tech Stack
Backend
Flask
Flask-SocketIO
Gevent
AI
Groq API (Llama 3.3 70B)
Prompt-engineered classification
Safety Engine
Regex detection
Keyword scoring
Risk aggregation logic
Communication
WebSockets
SMTP Email
📁 Project Structure
Online_Grooming/
│
├── app_og.py              # Main backend + AI + sandbox logic
├── templates/
│   └── chat_og.html      # Frontend chat UI
│
├── evidence_*.json       # Logs of chat sessions
├── render.yaml           # Deployment config
├── requirements.txt
├── runtime.txt
├── .gitignore
└── README.md
⚙️ Setup Instructions
1. Clone repo
git clone <repo-url>
cd Online_Grooming
2. Install dependencies
pip install -r requirements.txt
3. Add environment variables

Create .env:

SMTP_EMAIL=your_email
SMTP_PASSWORD=your_password
PARENT_EMAIL=parent_email
GROQ_API_KEY=your_key
RESEND_API_KEY=your_key

4. Run server
python app_og.py

🛡️ Security Features
No hardcoded API keys (env-based)
LLM + regex hybrid detection
Isolation + secrecy detection
Sandbox containment mode
Risk escalation tracking
Evidence logging for auditing

💡 Future Improvements
Train custom grooming classifier model
Add browser extension monitoring
Dashboard for risk analytics
Multi-language grooming detection
Replace prompt-only LLM with fine-tuned model
Graph-based conversation risk tracking



AI-based child safety research project focused on detecting and preventing online grooming using real-time LLM + rule-based hybrid systems.

⚠️ Disclaimer

This system is a research/prototype safety tool and should not be used as the sole protective mechanism in production environments.

