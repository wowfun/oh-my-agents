from __future__ import annotations

import sys
from pathlib import Path

import questionary

from hagency_cli.workspace.skills import SkillLinkCandidate


class QuestionarySkillConflictUI:
    """Skill selection and conflict prompts backed by Questionary."""

    def is_interactive(self) -> bool:
        return sys.stdin.isatty() and sys.stdout.isatty()

    def select(
        self, name: str, candidates: tuple[SkillLinkCandidate, ...]
    ) -> Path | None:
        try:
            prompt = questionary.select(
                f"Choose the source for skill {name!r}",
                choices=[
                    questionary.Choice(
                        title=f"{candidate.source_name}: {candidate.target}",
                        value=candidate.target,
                    )
                    for candidate in candidates
                ],
            )
            return prompt.unsafe_ask()
        except (EOFError, KeyboardInterrupt):
            return None

    def choose_skills(
        self, candidates: tuple[SkillLinkCandidate, ...]
    ) -> tuple[Path, ...] | None:
        try:
            result = questionary.checkbox(
                "Choose skills to install",
                choices=[
                    questionary.Choice(
                        title=f"{candidate.name}: {candidate.target}",
                        value=candidate.target,
                        checked=False,
                    )
                    for candidate in candidates
                ],
            ).unsafe_ask()
            return tuple(result) if result is not None else None
        except (EOFError, KeyboardInterrupt):
            return None


__all__ = ["QuestionarySkillConflictUI"]
