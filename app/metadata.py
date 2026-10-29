"""Single source of truth for Helix product metadata.

Everywhere else should import from here rather than hard-coding strings.
Keeps CLAUDE.md's rename-safe invariant honest: product name = Helix,
distribution/package id = PCHealthAgent.
"""

APP_NAME     = "Helix"
APP_ID       = "PCHealthAgent"
APP_VERSION  = "1.1.2"
GITHUB_URL   = "https://github.com/TW-10517/HealthAgent"
WEBSITE_URL  = "https://helix-57o.pages.dev"
UPDATE_MANIFEST_URL = WEBSITE_URL + "/version.json"
WINDOW_TITLE = APP_NAME
