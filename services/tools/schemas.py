"""
Tool/function-call declarations in OpenAI `tools=[...]` format.

Two distinct tool sets:
  • ESCALATE_TOOL        — the single tool the fast SLM (Groq) may call to hand
                           a turn off to the tool-calling LLM.
  • TASK_TOOL_DECLARATIONS — the real action tools the LLM (OpenRouter) can call.
                           (`research` is added in Milestone 3.)
"""

# ── SLM routing tool ─────────────────────────────────────────────────────
ESCALATE_TOOL = {
    "type": "function",
    "function": {
        "name": "escalate_to_assistant",
        "description": (
            "Hand off to the advanced assistant ONLY when the user wants to "
            "create, update, complete, or list/look up a task or reminder. "
            "Do NOT call this for greetings, casual chat, or general/factual "
            "questions — answer those yourself directly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent_summary": {
                    "type": "string",
                    "description": "One sentence describing exactly what the user wants done.",
                },
            },
            "required": ["intent_summary"],
        },
    },
}

# ── LLM action tools ─────────────────────────────────────────────────────
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
                        "description": "Optional ISO 8601 date/time the task is due, if known.",
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
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_tasks",
            "description": "Look up the user's existing tasks/reminders to answer questions like 'what's on my list today'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["today", "this_week", "this_month", "all_active", "by_status", "specific_task"],
                        "description": "Which slice of tasks to return.",
                    },
                    "status_filter": {
                        "type": "string",
                        "enum": ["pending", "blocked", "active", "done", "cancelled"],
                        "description": "Used when scope=by_status.",
                    },
                    "search_text": {
                        "type": "string",
                        "description": "Used when scope=specific_task — fuzzy match against task titles.",
                    },
                },
                "required": ["scope"],
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


def get_tool_declarations() -> list[dict]:
    """The tool set advertised to the OpenRouter LLM."""
    return TASK_TOOL_DECLARATIONS + [RESEARCH_TOOL]
