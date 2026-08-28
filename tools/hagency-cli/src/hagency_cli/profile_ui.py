from __future__ import annotations

import sys
from pathlib import Path

import questionary

from .profiles import SkillLinkCandidate


class QuestionarySkillConflictUI:
    """Interactive profile skill conflict prompts backed by Questionary."""

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


__all__ = ["QuestionarySkillConflictUI"]
