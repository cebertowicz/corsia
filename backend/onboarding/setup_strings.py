"""Every user-facing string of a new install's setup decisions, in one module.

The conductor raises these decisions unchanged; the model never writes or edits
one. Braces are filled by ``setup_decisions``. Changing a word here changes the
decision, and ``test_setup_decisions`` asserts each string exactly.

A decision exists only for a real tradeoff or an approval the asker can act on;
everything else the conductor asks in chat.
"""

from __future__ import annotations

# FIND_PROJECTS option A authorizes reading session folders (onboarding.project_discovery).
# The consent hash binds the rendered title, context and option A label, so a changed word
# here needs a new consent review.

# --- task "Getting started": bring over what other AI tools know -----------

# AGENT_IMPORT option A authorizes one read of the person's other AI tools
# (onboarding.agent_import); its consent hash binds the rendered title, context and
# option A label like FIND_PROJECTS'. {tools} names the found tools, {verb} is
# "is" for one and "are" for several, {connections} is AGENT_IMPORT_CONNECTIONS
# or empty, {where} one of AGENT_IMPORT_CLOUD and AGENT_IMPORT_LOCAL.
AGENT_IMPORT = {
    "title": "Bring over what your other AI tools know",
    "context": (
        "{tools} {verb} on this Mac. I can read what they remember about you and how you like to "
        "work, the skills you keep in them{connections}, so you don't have to set them up again. "
        "I only read: nothing changes in them, and you choose what I keep. I never read your chats "
        "with them, their settings, or any key or password. {where}"
    ),
    "tradeoff": (
        "It takes about a minute. Not now means a few more questions here, and your skills stay "
        "where they are until you ask."
    ),
    "options": (
        ("Read them", "Then you pick what comes over."),
        ("Not now", "Ask me any time."),
    ),
    "rationale": "You've already taught them how you work; this saves teaching it again.",
}
# Only ready Claude Code or Codex tools hold connections this release can bring over.
AGENT_IMPORT_CONNECTIONS = ", and the connections to other tools that {tools} {verb}"
AGENT_IMPORT_CLOUD = "What I read goes to {provider}, the AI I run on."
AGENT_IMPORT_LOCAL = "What I read stays on this Mac: I run on a local AI."
# The provider each cloud harness or API names, by catalog provider.
AGENT_IMPORT_PROVIDERS = {
    "claude-code": "Claude, by Anthropic",
    "anthropic": "Claude, by Anthropic",
    "codex": "GPT, by OpenAI",
    "openai": "GPT, by OpenAI",
}

# --- task "Set up your first project" ------------------------------------

FIND_PROJECTS = {
    "title": "Look for the projects you already work on?",
    "context": (
        "I can find the projects you work on in {apps}. I read only the folder each session "
        "started in (the start of each session file), never your conversations, and propose "
        "which folders belong together as one project. You approve each project before I create "
        "it. That folder list goes to the model I run on. Nothing changes in {apps}."
    ),
    "tradeoff": (
        "Your folder names go to the model I run on. Describing the project yourself sends "
        "nothing, and takes a sentence or a folder path from you."
    ),
    "options": (
        ("Find my projects", "I'll propose your projects for you to approve."),
        ("I'll describe it", "Tell me the folder or a sentence about the work."),
    ),
    "rationale": "It starts from the work you already do, and nothing is created until you approve it.",
}

# --- task "Configuration checks": oMLX setting ----------------------

OMLX = {
    "title": "Change one oMLX setting for CORSIA?",
    "context": (
        "Your oMLX runs the local models on this Mac. It's set to work on {current} requests at "
        "once. At {recommended}, oMLX finishes one request before it starts the next, so each one "
        "gets the model's full speed and memory instead of sharing them. I'd change only this "
        "one setting."
    ),
    "tradeoff": (
        "Requests that arrive together, from CORSIA's agents or from other apps that use your "
        "oMLX, will wait their turn instead of running alongside each other. You can change it "
        "back in oMLX's settings at any time."
    ),
    "yes_label": "Change it to {recommended}",
    "yes_detail": "I'll apply it now and tell you if oMLX needs a restart.",
    "no_label": "Leave it at {current}",
    "no_detail": "CORSIA still works; requests that arrive together share the model.",
    "rationale": (
        "It matches how CORSIA sets up an oMLX it installs. Choose B if other apps use your oMLX "
        "all day."
    ),
}

# While the person lets CORSIA manage their oMLX, the decision lists every change:
# {changes} is OMLX_GROUP_GLOBAL and OMLX_GROUP_MODEL joined by "; ", each of OMLX_ROW.
OMLX_MANAGED = {
    "title": "Change your oMLX's settings for CORSIA?",
    "context": (
        "You let CORSIA manage your oMLX. I'd change these settings, each shown as current → new: "
        "{changes}. They change for every app that uses this oMLX. Your key, sign-in, network "
        "settings, port, folders, default model and other models stay as they are."
    ),
    "tradeoff": (
        "Other apps that use your oMLX get these settings too. Stopping management in Settings "
        "puts every value back."
    ),
    "yes_label": "Change them",
    "yes_detail": "I'll apply them now and tell you if oMLX needs a restart.",
    "no_label": "Leave them as they are",
    "no_detail": "CORSIA still works with your current settings.",
    "rationale": "They match how CORSIA sets up an oMLX it installs.",
}
OMLX_GROUP_GLOBAL = "oMLX-wide, {rows}"
OMLX_GROUP_MODEL = "for {model}, {rows}"
OMLX_ROW = "{label}: {current} → {recommended}"
# What Settings' "Stop managing" puts back, one line per recorded value ("CORSIA puts back …").
OMLX_RESTORE = "{label} to {value}"
OMLX_RESTORE_MODEL = "{label} for {model} to {value}"
# oMLX's own admin-page names, so the person finds each one there.
OMLX_LABELS = {
    "max_concurrent_requests": "Max Concurrent Requests",
    "prefill_priority": "Prefill Priority",
    "max_context_window": "Ctx Window",
    "max_tokens": "Max Tokens",
    "turboquant_kv_enabled": "TurboQuant KV Cache",
    "turboquant_kv_bits": "TurboQuant bits per channel",
    "turboquant_skip_last": "TurboQuant skip last layer",
    "mtp_enabled": "Native MTP",
    "enable_thinking": "Thinking",
}
OMLX_DEFAULT = "oMLX default"


def omlx_value(value: object) -> str:
    """A setting's value as the person reads it: unset is oMLX's default, a switch on or off."""
    if value is None:
        return OMLX_DEFAULT
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


# --- task "Set up GitHub" ----------------------------------------------------------

# {sign_up_with} is the agents' address, or ASK_FOR_ADDRESS when the saved email is not
# Gmail: on option A the conductor then asks in chat for an address.
ASK_FOR_ADDRESS = "the address you give me in chat"

GITHUB = {
    "title": "How should your agents post on GitHub?",
    "context": (
        "Agents use GitHub to file issues and feature requests, comment, and send pull requests: "
        "on your own repositories, and on CORSIA's when it falls short and I can fix it. They can "
        "post as you, using your GitHub sign-in, or from their own account, so their posts stay "
        "separate from yours. GitHub allows each person one extra machine account for this."
    ),
    "tradeoff": (
        "Posting as you takes one sign-in, and everything agents post shows your name. Their own "
        "account takes a few minutes in a browser you can watch: I fill in the sign-up with "
        "{sign_up_with}, and you type the password and any verification code."
    ),
    "options": (
        ("Their own account",
         "I open the browser and fill in the sign-up. You type the password and any code."),
        ("Post as me",
         "I sign the GitHub CLI in to your account. You approve that sign-in in your browser."),
        ("Not now", "Ask me any time."),
    ),
    "rationale": "You can always tell what you wrote from what an agent wrote.",
}

# --- task settlement the conductor applies without asking ---------------------

WEB_SEARCH_WORKS = "Web search already works."
