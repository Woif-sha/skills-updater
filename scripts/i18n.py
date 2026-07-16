#!/usr/bin/env python3
"""Strict English and Chinese strings for skills-updater CLI output."""

from __future__ import annotations

import locale
import os
from typing import Optional


TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "checking_updates": "Checking for skill updates...",
        "installed_skills_status": "Installed Skills Status",
        "up_to_date": "Up-to-date",
        "updates_available": "Updates Available",
        "local_only": "Local-only (remote updates disabled)",
        "unknown_version": "Unknown Version",
        "errors": "Errors",
        "total": "Total",
        "skills": "skills",
        "updates_available_count": "updates available",
        "skill_not_found": "Skill '{skill}' not found.",
        "no_installed_skills": "No installed skills found.",
        "local": "Local",
        "remote": "Remote",
        "warning": "Warning",
    },
    "zh": {
        "checking_updates": "正在检查技能更新...",
        "installed_skills_status": "已安装技能状态",
        "up_to_date": "已是最新",
        "updates_available": "有可用更新",
        "local_only": "仅本地（已禁用远程更新）",
        "unknown_version": "版本未知",
        "errors": "错误",
        "total": "总计",
        "skills": "个技能",
        "updates_available_count": "个可更新",
        "skill_not_found": "未找到技能 '{skill}'",
        "no_installed_skills": "未找到已安装的技能",
        "local": "本地",
        "remote": "远程",
        "warning": "警告",
    },
}

_language: Optional[str] = None


def detect_locale() -> str:
    """Return Chinese for an explicit Chinese locale; English otherwise."""
    for name in ("LANG", "LC_ALL", "LANGUAGE", "LC_MESSAGES"):
        value = os.environ.get(name)
        if value:
            normalized = value.casefold()
            return "zh" if normalized.startswith("zh") or "chinese" in normalized else "en"

    system_locale = locale.getlocale()[0] or ""
    return "zh" if system_locale.casefold().startswith("zh") else "en"


def get_i18n(lang: Optional[str] = None) -> str:
    """Select a supported language and return its code."""
    global _language
    selected = lang or _language or detect_locale()
    if selected not in TRANSLATIONS:
        raise ValueError(f"Unsupported language: {selected}")
    _language = selected
    return selected


def t(key: str, **kwargs: object) -> str:
    """Format a registered translation; missing keys or arguments are errors."""
    return TRANSLATIONS[get_i18n()][key].format(**kwargs)
