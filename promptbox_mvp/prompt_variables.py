"""Shared Prompt variable parsing and rendering."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


@dataclass(frozen=True)
class PromptVariable:
    """One variable used by a Prompt template."""

    name: str
    description: str = ""
    example: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "description": self.description,
            "example": self.example,
        }


class PromptTemplate:
    """Parse a Prompt and render it with one shared set of variable values."""

    def __init__(self, text: str, variables: list[PromptVariable]):
        if not isinstance(text, str):
            raise ValueError("Prompt 模板必须是字符串")
        self.text = text
        self._variables = tuple(variables)

    @classmethod
    def from_text(
        cls,
        text: str,
        definitions: dict[str, dict[str, Any]] | None = None,
    ) -> "PromptTemplate":
        if not isinstance(text, str):
            raise ValueError("Prompt 模板必须是字符串")
        definitions = definitions or {}
        names = list(dict.fromkeys(_PLACEHOLDER_RE.findall(text)))
        variables = []
        for name in names:
            definition = definitions.get(name) or {}
            variables.append(
                PromptVariable(
                    name=name,
                    description=str(definition.get("description") or ""),
                    example=str(definition.get("example") or ""),
                )
            )
        return cls(text, variables)

    @property
    def variable_names(self) -> list[str]:
        return [variable.name for variable in self._variables]

    @property
    def variables(self) -> list[dict[str, str]]:
        return [variable.as_dict() for variable in self._variables]

    def render(self, values: dict[str, str]) -> str:
        if not isinstance(values, dict):
            raise ValueError("变量填写内容必须是字典")
        for name in self.variable_names:
            value = values.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"变量未填写：{name}")

        rendered = self.text
        for name in self.variable_names:
            rendered = rendered.replace("{" + name + "}", values[name])
        return rendered

    def definitions(self) -> dict[str, dict[str, str]]:
        return {
            variable.name: {
                "description": variable.description,
                "example": variable.example,
            }
            for variable in self._variables
        }

    def copy(self) -> "PromptTemplate":
        return PromptTemplate(self.text, deepcopy(list(self._variables)))


__all__ = ["PromptTemplate", "PromptVariable"]
