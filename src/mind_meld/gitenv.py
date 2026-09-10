"""Environment for Git reads whose repository is chosen by filesystem discovery.

Discovery ignores Git's repository-local environment, so the subprocess must
ignore it too or it can attribute another repository's history and identities
to the discovered root. Mirror Git's ``rev-parse --local-env-vars`` list;
``test_repo_local_env_vars_cover_installed_git`` detects upstream additions.

Keep user/platform settings: PATH, HOME, GIT_EXEC_PATH, GIT_CONFIG_GLOBAL,
GIT_CONFIG_SYSTEM, GIT_CONFIG_NOSYSTEM, GIT_SSH*, and XDG_CONFIG_HOME. Unlike pre-commit's policy,
GIT_CONFIG_COUNT/KEY/VALUE and GIT_CONFIG_PARAMETERS are redirection for mm's
attribution-sensitive reads. Use ~/.gitconfig or GIT_CONFIG_GLOBAL for lasting
configuration. Pin the message locale so empty repositories remain benign.
"""

import os
from collections.abc import Mapping

GIT_REPO_LOCAL_ENV_VARS: frozenset[str] = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_OBJECT_DIRECTORY",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
        "GIT_PREFIX",
        "GIT_SHALLOW_FILE",
        "GIT_COMMON_DIR",
    }
)
_GIT_CONFIG_ITEM_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def scrubbed_git_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy the current (or supplied) environment without repository overrides."""
    source = os.environ if base is None else base
    env = {
        name: value
        for name, value in source.items()
        if name not in GIT_REPO_LOCAL_ENV_VARS and not name.startswith(_GIT_CONFIG_ITEM_PREFIXES)
    }
    env.update(LC_ALL="C", LANGUAGE="C")
    return env
