"""
Tool/function-call declarations in OpenAI `tools=[...]` format — the action
tools the brain (Gemini) can call. `research` reaches out to OpenRouter under
the hood; everything else runs against our own DB.
"""

# ── Action tools ─────────────────────────────────────────────────────────
TASK_TOOL_DECLARATIONS = [
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": (
                "Create a task or reminder for the user. Use for single-step "
                "reminders ('recharge my phone Friday') and for multi-step goals "
                "(pass a parent_task to nest a sub-step under an existing task)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short title of the task."},
                    "description": {"type": "string", "description": "Optional extra detail."},
                    "due_at": {
                        "type": "string",
                        "description": (
                            "ISO 8601 date/time the task is due, in INDIAN time (IST, "
                            "+05:30) unless the user's profile says otherwise. When the "
                            "user gives a CLOCK TIME ('9pm', '3pm tonight', 'noon'), "
                            "ALWAYS include the full time component — never collapse to "
                            "midnight or date-only. Example: '2026-06-25T21:00:00+05:30'."
                        ),
                    },
                    "reminder_offsets_minutes": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "When to fire reminder notifications, as minutes BEFORE "
                            "due_at. 0 means at the event time. Default is [0, 10] (one at "
                            "the time and one 10 minutes before) — you usually DON'T need "
                            "to set this. Set it ONLY when the user asks for a specific "
                            "lead time: 'remind me 30 minutes before' → [0, 30]; 'remind "
                            "me a day before and at the time' → [0, 1440]; 'just at the "
                            "time' → [0]."
                        ),
                    },
                    "window_start": {
                        "type": "string",
                        "description": (
                            "Optional ISO 8601 datetime — start of a fuzzy time window "
                            "when the user gives a loose time phrase ('Saturday evening', "
                            "'sometime tomorrow afternoon') where a single due_at would "
                            "overstate precision. Use INSTEAD of due_at, not alongside it."
                        ),
                    },
                    "window_end": {
                        "type": "string",
                        "description": (
                            "Optional ISO 8601 datetime — end of the fuzzy time window. "
                            "Always pair with window_start."
                        ),
                    },
                    "parent_task": {
                        "type": "string",
                        "description": "Optional title or id of an existing task to nest this under.",
                    },
                    "depends_on_task": {
                        "type": "string",
                        "description": "Optional title or id of a task that must be done first.",
                    },
                    "needs_research": {
                        "type": "boolean",
                        "description": "True if external/current info is needed to fill in dates/details.",
                    },
                    "research_query": {
                        "type": "string",
                        "description": (
                            "When needs_research is true, the EXACT search query to use when "
                            "the scheduler polls for this task in the background. Be specific "
                            "(e.g. 'JLPT N5 registration link India December 2026 session')."
                        ),
                    },
                    "success_condition": {
                        "type": "string",
                        "description": (
                            "When needs_research is true, what constitutes a successful search "
                            "result (e.g. 'Find a valid registration URL', 'Confirm the exam date'). "
                            "The scheduler uses this to decide whether to keep retrying."
                        ),
                    },
                    "retry_interval_days": {
                        "type": "integer",
                        "description": (
                            "When needs_research is true and the search fails, how many days to "
                            "wait before retrying. Default: 7."
                        ),
                    },
                    "user_confirmed": {
                        "type": "boolean",
                        "description": (
                            "TRUE only if the user EXPLICITLY asked you to create/track/"
                            "remind this, or said yes to your offer. FALSE if you are only "
                            "inferring it would help — when FALSE the task is NOT created and "
                            "you must ask the user to confirm first. Never default to TRUE."
                        ),
                    },
                    "auto_archive_after_hours": {
                        "type": "integer",
                        "description": (
                            "For SHORT-LIVED, throwaway reminders ('get eggs tomorrow', "
                            "'call mom tonight'), set this to how many hours after the due "
                            "time the reminder should auto-clear if not done — e.g. 24. This "
                            "keeps trivial one-off reminders from lingering forever. For "
                            "lasting goals or anything worked toward over time ('train for "
                            "the marathon', 'register for the exam'), OMIT it so the task "
                            "persists."
                        ),
                    },
                    "research_summary": {
                        "type": "string",
                        "description": "If you called the research tool first, the key findings to store on this task.",
                    },
                    "source_links": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Official/source URLs from research to store on this task.",
                    },
                },
                "required": ["title", "user_confirmed"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_tasks",
            "description": (
                "Look up the user's existing tasks/reminders to answer ANY question "
                "about them — what's due, when a specific task is due, what's overdue, "
                "when a task was created, or its details. Always call this before "
                "answering a question about tasks; never answer from memory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["today", "this_week", "this_month", "all_active", "overdue", "by_status", "specific_task"],
                        "description": (
                            "Which slice to return. Use `specific_task` to look up one "
                            "task by name, `by_status` to filter by status, `overdue` "
                            "for past-due tasks, or a time horizon. For an explicit date "
                            "range (e.g. 'next month', 'in December'), use `all_active` "
                            "together with due_after/due_before."
                        ),
                    },
                    "status_filter": {
                        "type": "string",
                        "enum": ["pending", "blocked", "active", "done", "cancelled"],
                        "description": "Used when scope=by_status.",
                    },
                    "search_text": {
                        "type": "string",
                        "description": "Used when scope=specific_task — fuzzy match against task titles (e.g. 'Cairo train').",
                    },
                    "due_after": {
                        "type": "string",
                        "description": "Optional ISO 8601 date/time — only return tasks due on or after this. Compute it from today for ranges like 'next month'.",
                    },
                    "due_before": {
                        "type": "string",
                        "description": "Optional ISO 8601 date/time — only return tasks due on or before this.",
                    },
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": (
                "Amend an EXISTING task's details — add or correct its link, fee, "
                "due date, description, or link it to a parent/prerequisite task. "
                "Use this (NOT create_task) whenever the user asks to add "
                "information to, correct, or extend something already tracked — "
                "calling create_task again would duplicate it instead of updating it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Title or id of the existing task to update (fuzzy-matched).",
                    },
                    "title": {"type": "string", "description": "Optional new title."},
                    "description": {"type": "string", "description": "Optional new/extra description."},
                    "due_at": {
                        "type": "string",
                        "description": (
                            "Optional new ISO 8601 due date/time in IST (+05:30), "
                            "including time-of-day if given. Changing it re-schedules "
                            "this task's reminder notifications."
                        ),
                    },
                    "reminder_offsets_minutes": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "Optional — only when also changing due_at AND the user "
                            "wants specific lead times. Minutes before due_at; 0 = at the "
                            "time. Defaults to [0, 10] when omitted."
                        ),
                    },
                    "window_start": {"type": "string", "description": "Optional new fuzzy-window start."},
                    "window_end": {"type": "string", "description": "Optional new fuzzy-window end."},
                    "parent_task": {
                        "type": "string",
                        "description": "Optional title/id of an existing task to nest this under.",
                    },
                    "depends_on_task": {
                        "type": "string",
                        "description": (
                            "Optional title/id of a prerequisite task — set this when the user "
                            "links this task to something already tracked, even from an earlier "
                            "turn (e.g. 'participate in the race' depends on 'register for it')."
                        ),
                    },
                    "research_summary": {
                        "type": "string",
                        "description": "New/updated findings to store — merged with anything already saved, not overwritten.",
                    },
                    "source_links": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New/updated official URLs to store — merged with anything already saved.",
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task_status",
            "description": "Update the status of an existing task, e.g. when the user says they've completed or cancelled it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Title or id of the task to update (fuzzy-matched against active tasks).",
                    },
                    "new_status": {
                        "type": "string",
                        "enum": ["pending", "blocked", "active", "done", "cancelled"],
                    },
                    "note": {"type": "string", "description": "Optional note about why it changed."},
                },
                "required": ["task", "new_status"],
            },
        },
    },
]


RESEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "research",
        "description": (
            "Look up current, real-world information using live web search — exam "
            "dates, deadlines, prices, schedules, requirements, etc. Call this "
            "BEFORE create_task whenever a task depends on facts or dates you don't "
            "already know, then pass the findings into create_task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A specific, self-contained search query.",
                },
            },
            "required": ["query"],
        },
    },
}


UPDATE_PROFILE_TOOL = {
    "type": "function",
    "function": {
        "name": "update_profile",
        "description": (
            "Save a STABLE personal detail about the user so future answers are "
            "personalized without them repeating it. Call this as soon as the user "
            "tells you where they're based (city/region/country) or their timezone — "
            "e.g. 'I'm in Delhi'. Don't use it for one-off task details."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "The user's city/region/country, e.g. 'Delhi, India'.",
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone, e.g. 'Asia/Kolkata', if stated or clearly implied by the location.",
                },
            },
        },
    },
}


def get_tool_declarations() -> list[dict]:
    """The full tool set advertised to the SLM."""
    return TASK_TOOL_DECLARATIONS + [RESEARCH_TOOL, UPDATE_PROFILE_TOOL]
