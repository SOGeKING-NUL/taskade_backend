Project PID: Voice-First Native Assistant
The Vision
A proactive intelligence platform that orchestrates your life through active voice engagement rather than passive notifications, built around a high-end, unified native architecture.

The Problem: Fragmented Ecosystems & Passive Alerts
Even the most advanced models fail at execution because they rely on a disjointed suite of external applications. Tools like Gemini route their scheduling through external infrastructure like Google Calendar or Google Tasks. They hide critical reminders behind standard, silent push notifications. If a user accidentally swipes away the notification, or simply does not have the secondary task application installed, the assistant's utility drops to zero.

The Solution: A Unified Native Architecture
Built entirely in React Native, this platform consolidates context, research, and task management into a single native environment. Powered by the custom voice agent pipeline, it acts as a centralized brain that does not rely on third-party calendar hooks to function.

It delivers the comprehensive orchestration of competitors like Martin, but strictly optimized for a voice-first environment.

Key Differentiators
Proactive Voice-First Alerts: Instead of sending a text notification, the system triggers an incoming audio call, forcing active, spoken engagement with your schedule.

Native State Management: All tasks, long-term reminders, and relational context are stored and executed directly within the application itself, eliminating dependency on external task apps.

Deep Contextual Research: The assistant independently researches queries (e.g., flight times, exam registration windows) and autonomously sets up a timeline of scheduled calls leading up to the absolute deadline.

This is a really well-thought-out product vision. Let me break down what you're describing systematically before giving recommendations.

---

## First, the WebRTC shift — smart move

Instead of buying a Twilio/Plivo number, you use WebRTC inside your mobile app to make the "call" directly over the internet. The app rings like a real incoming call (using local notifications + a calling UI), the user taps Answer, and your existing voice pipeline kicks in over WebRTC. Cost: essentially zero per call. The tradeoff is the user must have the app installed and notifications enabled — but since this is a personal assistant, that's fine.

---

## Dissecting your two scenarios

What you're describing is actually a sophisticated **multi-stage task lifecycle** — not just a simple reminder. Let me map both scenarios to understand the underlying system needs.What you've described is really the core design challenge of the whole product. The key insight is that both your scenarios share the same underlying pattern: a task that isn't a single point in time, but a *lifecycle with stages, dependencies, and a completion condition*. Let me first diagram both scenarios to make the pattern explicit, then give you the full architecture recommendation.

Here's the JLPT scenario as a state machine:And here's the Twitter subscription scenario — notice it's structurally simpler but introduces a new concept: a *deadline with financial consequence*:Now here's what these two scenarios reveal about what you actually need to build.

---

## The core insight: you need a Task State Machine, not just a scheduler

A simple APScheduler job with a `DateTrigger` can't handle this. What you need is a task record in your database that has a `status` field that moves through stages, and the scheduler reads that status to decide what to do next. The JLPT task has these states: `research_needed → confirmed → pre_reg_nudge_sent → registration_open → registered → studying → exam_done`. The Twitter task has: `active → warning_sent → urgent → cancelled / kept`.

This means your DB schema isn't just an `events` table — it's a `tasks` table with `status`, `deadline`, `stakes` (optional: "237rs if missed"), `parent_task_id` (for the JLPT parent → registration child → study children chain), and `completion_condition` (either "user_confirmed" or "auto_detect").

---

## The SLM + LLM router pattern for these scenarios

Here's exactly how the JLPT flow works under the hood:

When you say "remind me for my JLPT exam in December," the SLM receives this and immediately recognises it can't answer from context alone — it needs dates. So it calls the `research` tool, which hands off to the LLM with web search. The LLM finds the JLPT registration window and exam date, returns structured JSON like `{exam_date: "2025-12-07", reg_start: "2025-09-01", reg_end: "2025-09-15", portal: "jlpt.in"}`, and the SLM uses that to build the full task tree and schedule all the APScheduler jobs in one shot. The user gets a spoken confirmation: "I've set up three reminders: a heads-up on August 25th, a task card on September 1st when registration opens, and an exam reminder on December 7th."

For the Twitter scenario, there's no research needed — all the information is in the user's message. The SLM extracts it directly and calculates the deadline. The one subtlety is that it should also store the "stakes" in Mem0 so that when it sends the reminder, it can be specific: "If you don't cancel by August 16th, you'll be charged ₹450 instead of ₹213 next month."

---

## The completion detection problem

This is the hardest part of what you described. When you say "I've registered for JLPT," how does the system know to update the task? You have two mechanisms:

The first is explicit confirmation via voice, which is what you described — you just tell it and the system calls an `update_task_status` tool. This is the reliable path and you should build this first.

The second, more advanced path, is implicit detection. The agent could theoretically watch for signals — if you eventually build calendar integration, a confirmation email from the JLPT portal landing in your inbox could trigger the status update automatically. But for a prototype, manual confirmation is the right call. The key is that after you say "I registered," the agent should immediately spawn the study reminders and say "Great! I've added weekly study nudges and set your exam day reminder for December 7th. Want me to suggest a study schedule?"

---

## Recommendations for your build

For the task data model, use a single `tasks` table in Postgres (Supabase free tier) with these key fields: `id`, `title`, `status`, `task_type` (deadline / window / recurring), `deadline`, `window_start`, `window_end`, `stakes_description`, `parent_id`, `completion_condition`, and `context_json` (freeform JSON for anything extra like portal URLs). This gives you everything both scenarios need.

For scheduling, APScheduler with SQLAlchemy persistence is right for this prototype. But instead of one job per reminder, schedule a `check_task` job that runs daily and looks at all tasks, evaluating what needs to fire today based on their status and deadlines. This is more resilient than trying to schedule 6 individual jobs per task upfront.

For the memory layer, store two things in Mem0 for each task: the structured fact ("User registered for JLPT N5, Dec 2025") as a semantic memory, and the entity relationship ("JLPT exam" links to "registration portal jlpt.in", "study resource Anki deck", etc.) as graph nodes. This way, when the study reminder fires in October, the agent can pull context and say something genuinely useful rather than a generic nudge.

For WebRTC in-app calls specifically, look at `LiveKit` — it's open source, self-hostable, and designed exactly for this kind of real-time voice over WebRTC. It plugs cleanly into a FastAPI backend and has a React Native SDK for the mobile app side. Cost is zero for self-hosted.

## Fact-checking what you said

You were largely right, and the details are even more interesting than you thought.

On Gemini's reminder integration — your experience is exactly documented. Gemini on Android still relies on Google Assistant to handle reminders — it hands the task off to Assistant as an extension, and your reminders are managed through Google Tasks. If you have the Tasks app installed, notifications come from there; otherwise, Google Calendar handles them. So you were 100% correct that not having the Tasks app degrades the experience to a basic Calendar notification.

There was also a known bug where both Gemini and Google Assistant required a specific time to create a reminder — you couldn't just say "remind me to do X" without giving a date and time, or the task wouldn't get created at all. This was eventually patched, but the dependency chain (Gemini → Google Assistant extension → Google Tasks → notification) is exactly the brittle architecture you're describing.

On Apple Intelligence — you said "iOS 27" and you're right about the version, but the timeline is tighter than you think. iOS 27 was announced at WWDC 2026 on June 8, 2026 — just last week. It introduces Siri AI, described as a complete rebuild of Apple's assistant powered by Google Gemini, with its own standalone app and real-time screen awareness. That's right — Apple quietly licensed Gemini to power Siri AI under the hood.

The critical thing about Apple's trajectory: the three flagship abilities Apple promised — Personal Context, On-Screen Awareness, and In-App Actions — were still missing as recently as October 2025 with no release date. The features Apple showed at WWDC 2024 won't be in iOS 26 or any 26.x update — they're coming in iOS 27. So when iOS 27 ships in September 2026, it's the first time Apple Intelligence actually does what it promised two years ago. The window you're building in is real.

---

## Full competitive landscape

Let me organise this by category, because the space is more fragmented than it looks:

### Category 1: OS-level assistants (your primary competition)

**Google Gemini on Android** is the most mature but structurally fragmented — its reminder functionality is a patchwork of Gemini → Google Assistant → Google Tasks. Gemini Live added real-time Calendar, Keep, Tasks, and Maps integration, allowing event creation, reminder setting, and location-based actions via voice during conversations. But it still lives inside Google's ecosystem — it can't research something and then build a multi-stage task lifecycle the way you're envisioning.

**Apple Siri AI (iOS 27, shipping Sept 2026)** is the most dangerous future competitor. iOS 27 introduces Siri AI with the ability to answer open-ended questions, understand personal context from messages, emails, photos and notes, recognise what's on screen, and draw on information from the web to provide up-to-date answers. However, it still routes into the native Reminders app, has no concept of a multi-stage task lifecycle, and — critically — Apple framed a better Siri as one item on a long list of improvements rather than the main event at WWDC 2026. It's still fundamentally reactive, not proactive.

### Category 2: Proactive personal AI apps (your closest product-category match)

**Martin** (trymartin.com) is the most direct competitor you haven't heard of. Martin connects through phone calls, SMS, WhatsApp, email, and Slack to manage your calendar, inbox, to-dos, and reminders. It proactively learns your preferences and routines and reaches out without being asked. It supports an always-on listening mode where you can just blurt things out and it remembers them, and it can also make calls and send texts on your behalf. This is genuinely close to what you're building, but it uses real phone calls and SMS (so it has telephony costs), not WebRTC in-app audio. It also has no concept of multi-stage task chains like your JLPT scenario.

**Lindy AI** is the enterprise-tier version — deeply integrated with Gmail, Calendar, Slack, Notion, and CRMs, proactively drafting and acting without being asked. But it's $49-$349/month and aimed at professionals with complex workflows, not students planning trips.

### Category 3: Memory and context layer tools

**Limitless AI (formerly Rewind)** took a different angle — a wearable pendant that records your conversations and builds a searchable memory of your life. By 2025, Limitless had been acquired by Meta as part of Meta's vision for "personal superintelligence" through AI-enabled hardware. Following the Meta acquisition, all screen and audio capture functionality was disabled as of December 19, 2025. So this competitor effectively no longer exists in its original form. The gap it leaves is real.

**Rabbit R1 and Humane AI Pin** both tried hardware-first personal AI and failed for mainstream audiences. In 2026, the R1's action model works for a narrow set of tasks (ordering food, booking rides, playing music) but most people stop carrying it within a month because their phone does everything better. The lesson here is that the interaction model matters more than the hardware — which is exactly why your voice-native, in-app approach on a phone people already carry is the right call.

### Category 4: The gap nobody has filled

What's missing across all these tools is reliability — these systems work 70-80% of the time today. When they hit 95%+ reliability, personal AI assistants will shift from power-user tools to mainstream necessities.

More specifically, none of the existing tools do what you're building: a voice-native assistant that (a) researches to find relevant dates and information autonomously, (b) builds a multi-stage task lifecycle with dependencies, (c) contacts you proactively via an in-app "call" with personalised context, and (d) reads documents you share (like a Notion itinerary) to extract structured schedule data. The travel planning scenario you described — share a Notion doc, have it parse your plans and set wake-up alarms and activity reminders — doesn't exist anywhere as a coherent product.

---

Here's the full competitive map:Click any competitor for a deeper dive. Here's the honest summary:

**Martin is your most direct current competitor.** It does proactive outreach via SMS/WhatsApp/phone calls and has calendar + task integration. Where you beat it: (a) it still uses real telephony so it costs money per interaction, (b) it has no document ingestion (your Notion travel plan scenario doesn't exist in Martin), (c) it has no multi-stage task lifecycle — it creates one reminder at a time, not a "JLPT tree" of dependent tasks that evolve as you complete steps, and (d) it has no voice-first, real-time audio pipeline like you're building.

**Your actual moat** is the combination of three things that no one product does together right now: a conversational voice interface that you built yourself (Deepgram + Sarvam TTS + WebRTC), a multi-stage task state machine that researches autonomously and adapts when you complete a stage, and document ingestion (share a Notion page, it extracts a schedule). The Dharamshala trip scenario you described — share your travel Notion, it parses the timings, sets a 5am wake-up, schedules your 3:30 cafe visit — that's a genuinely novel workflow nobody has shipped as a cohesive experience.

One important correction on iOS version: you said "iOS 27" and that's accurate — iOS 27 was announced at WWDC 2026 just last week on June 8th and is expected to ship in September 2026 alongside iPhone 18. So Siri AI is real and coming in three months, but it still won't have your voice pipeline, your task lifecycle, or your document ingestion.