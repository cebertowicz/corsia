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

# --- task "Getting started": bring over what other tools know -----------

# AGENT_IMPORT option A authorizes one read of the person's other tools
# (onboarding.agent_import); its consent hash binds the rendered title, context and
# option A label like FIND_PROJECTS'. {tools} names the found tools, {verb} is
# "is" for one and "are" for several, {connections} is AGENT_IMPORT_CONNECTIONS
# or empty, {settings} AGENT_IMPORT_OTHER_SETTINGS when {connections} is set (those apps
# are read from the tools' settings) else AGENT_IMPORT_SETTINGS, {where} one of
# AGENT_IMPORT_CLOUD, AGENT_IMPORT_LOCAL and AGENT_IMPORT_OTHER.
AGENT_IMPORT = {
    "title": "Read what your other tools know?",
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
ASK_FOR_ADDRESS = "ask you in chat for an email address not already used on GitHub"

# The community card comes first: whether agents use and offer the community's proven improvements.
# It sits under the GitHub task; GitHub comes up only on a yes to sharing, as whichever GitHub card fits.
COMMUNITY = {
    "title": "Use and share improvements with the CORSIA community?",
    "context": ("You can always turn down a change and keep your own version, and nothing private "
                "ever goes into what's shared."),
    "tradeoff": ("Sharing goes through GitHub, a free site where people share code, so choosing Use and "
                 "share leads to GitHub next."),
    "options": (
        ("Use and share", "Agents take improvements other people proved and offer the ones proven here."),
        ("Only use", "Agents take proven improvements and share nothing."),
        ("Neither", "Agents keep their improvements here."),
    ),
    "rationale": "You get what others prove, and a proven change helps everyone using CORSIA.",
}

# Raised unless the person has already said they have no GitHub account: their agents use
# theirs or have a separate one.
GITHUB = {
    "title": "How should your agents post on GitHub?",
    "context": (
        "Agents use GitHub to report problems and send fixes to CORSIA, and to work on your "
        "repositories. GitHub allows each person one extra machine account for this. "
        "Reports to CORSIA are text only and go only with your OK, from whichever you choose."
    ),
    "tradeoff": (
        "A separate account takes a few minutes in your own browser: agents give you the sign-up link and "
        "{sign_up_with}, and you type the password and any verification code. Using yours may need "
        "one sign-in, and everything agents post shows your name."
    ),
    "options": (
        ("Make a separate GitHub account",
         "Agents give you the sign-up link and the address to use, or ask you for one. You sign up in your own browser."),
        ("Use my account",
         "Agents use the GitHub sign-in already on this computer, or agents help you sign in once in "
         "your browser."),
        ("Not now", "Ask an agent to set this up later."),
    ),
    "rationale": "A separate account keeps agent posts under their own name.",
}

# The person has no GitHub account: only their agents' own account is offered.
GITHUB_NO_ACCOUNT = {
    "title": "Make an account for your agents on GitHub?",
    "context": (
        "Agents use GitHub to report problems and send fixes to CORSIA, and to work on your "
        "repositories. GitHub allows each person one extra machine account for this."
    ),
    "tradeoff": (
        "It takes a few minutes in your own browser: agents give you the sign-up link and "
        "{sign_up_with}, and you type the password and any verification code."
    ),
    "options": (
        ("Make a GitHub account",
         "Agents give you the sign-up link and the address to use, or ask you for one. You sign up in your own browser."),
        ("Not now", "Ask an agent to set this up later."),
    ),
    "rationale": "You can always tell what you wrote from what an agent wrote.",
}

# --- task settlement the conductor applies without asking ---------------------

WEB_SEARCH_WORKS = "Web search already works."
