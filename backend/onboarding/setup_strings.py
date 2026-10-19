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
# or empty, {settings} AGENT_IMPORT_OTHER_SETTINGS when {connections} is set (those apps
# are read from the tools' settings) else AGENT_IMPORT_SETTINGS, {where} one of
# AGENT_IMPORT_CLOUD, AGENT_IMPORT_LOCAL and AGENT_IMPORT_OTHER.
AGENT_IMPORT = {
    "title": "Bring over what your other AI tools know",
    "context": (
        "{tools} {verb} on this computer. I can read what they remember about you and how you like to "
        "work, the skills you keep in them{connections}, so you don't have to set them up again. "
        "I only read: nothing changes in them, and you choose what I keep. I never read your chats "
        "with them, {settings}, or any key or password. {where}"
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
AGENT_IMPORT_CONNECTIONS = ", and the connected apps that {tools} {verb}"
AGENT_IMPORT_SETTINGS = "their settings"
AGENT_IMPORT_OTHER_SETTINGS = "their other settings"
AGENT_IMPORT_CLOUD = "What I read goes to {provider}, the model I run on."
AGENT_IMPORT_LOCAL = "What I read stays on this computer: I run on a local model."
# Any other model, an External endpoint included: who serves it is not known here.
AGENT_IMPORT_OTHER = "What I read goes to {model}, the model I run on, and may leave this computer."
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
