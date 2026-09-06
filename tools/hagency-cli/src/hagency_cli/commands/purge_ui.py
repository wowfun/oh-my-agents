from __future__ import annotations

import sys
from pathlib import Path

import questionary

from hagency_cli.commands.purge_render import format_bytes
from hagency_cli.files.purge.models import PurgeChoice


class QuestionaryPurgeUI:
    """Interactive purge prompts backed by Questionary."""

    def is_interactive(self) -> bool:
        return sys.stdin.isatty() and sys.stdout.isatty()

    def select(self, choices: tuple[PurgeChoice, ...]) -> tuple[str, ...] | None:
        prompt_choices: list[questionary.Choice | questionary.Separator] = []
        current_project: Path | None = None
        for choice in choices:
            if choice.project_path != current_project:
                current_project = choice.project_path
                prompt_choices.append(questionary.Separator(str(current_project)))
            prompt_choices.append(
                questionary.Choice(
                    title=choice.label,
                    value=choice.id,
                    checked=choice.preselected,
                )
            )
        prompt = questionary.checkbox(
            "Select project artifacts to purge",
            choices=prompt_choices,
        )
        try:
            selected = prompt.unsafe_ask()
        except EOFError:
            return None
        if selected is None:
            return None
        return tuple(selected)

    def confirm_exact(self, paths: tuple[Path, ...], known_bytes: int) -> bool:
        print("Selected paths:")
        for path in paths:
            print(f"  {path}")
        prompt = questionary.confirm(
            f"Remove {len(paths)} selected artifact(s) "
            f"({format_bytes(known_bytes)} known size)?",
            default=False,
        )
        try:
            confirmed = prompt.unsafe_ask()
        except EOFError:
            return False
        return bool(confirmed)


__all__ = ["QuestionaryPurgeUI"]
