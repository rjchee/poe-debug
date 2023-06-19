"""

Debugbot echoes back everything the server sent for easier debugging.

"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable, Generic, Optional, TypeVar

from aiohttp import web

from aiohttp_poe import PoeBot, run
from aiohttp_poe.types import (
    ContentType,
    Event,
    QueryRequest,
    ReportFeedbackRequest,
    SettingsResponse,
)
from collections import Counter
import copy
from dataclasses import dataclass
from enum import Enum
import json


ALLOWED_CONTENT_TYPES = {"text/markdown", "text/plain"}
ALL_COMMANDS = {
    "commands": "Lists available commands this bot can use.",
    "assign <option>=<value>": "Update a bot option.",
    "options": "Lists available options you can set and their current values.",
    "reset": "Resets the bot back to all default options.",
    'error [retries] "<message>"': (
        "Makes the bot emit an error back to the Poe server with the given "
        "error message (as a JSON string). The optional argument `retries` "
        "specifies the maximum number of times you want Poe to send the "
        "request. (Underneath, the bot server will keep track of the number "
        "of times it's seen this exact message and respond with "
        "allow_retry=False when there are no retries remaining). By default, "
        "`retries` is 0."
    ),
}

T = TypeVar("T")

@dataclass
class OptionsInfo(Generic[T]):
    description: str
    default_value: T
    parser: Callable[[T, str], T]


def _parse_bool(old_value: bool, value: str) -> bool:
    return value.lower() == "true"

def _parse_content_type(old_value: ContentType, value: str) -> ContentType:
    assert value in ALLOWED_CONTENT_TYPES, f"value must be in {ALLOWED_CONTENT_TYPES}"
    return value

def _parse_optional_nonnegative_int(old_value: Optional[int], value: str) -> Optional[int]:
    if value.lower() in {"none", "null"}:
        return None
    new_value = int(value)
    assert new_value >= 0, f"expected a non-negative integer, got {new_value}"
    return new_value

def _parse_list_str(old_value: list[str], value: str) -> list[str]:
    if value.startswith("["):
        new_value = json.loads(value)
        assert isinstance(
            new_value, list
        ), f"{key} should be a list, got {type(new_value)}"
        types = [type(r) for r  in new_value]
        assert all(t is bool for t in types), f"expected a list of strings, got {types}"
    elif value.startswith("+"):
        addition = json.loads(value[1:])
        assert isinstance(
            addition, str
        ), f"can only add strings to {key}, got {type(addition)}"
        assert addition not in old_value, f"{addition} is already in {key}"
        new_value = old_value + [addition]
    elif value.startswith("-"):
        to_remove = json.loads(value[1:])
        assert isinstance(
            to_remove, str
        ), f"can only remove strings from {key}, got {type(to_remove)}"
        new_value = [r for r in old_value if r != to_remove]
        assert (
            len(new_value) < len(old_value)
        ), f"tried to remove {to_remove} from {key} but it is not there"
    else:
        assert False, f"unknown format {value}"

    return new_value


class BotOptions(Enum):
    n_previous_messages = OptionsInfo[Optional[int]](
        description="Show up to N previous messages. If None, show all previous messages.",
        default_value=1,
        parser=_parse_optional_nonnegative_int,
    )
    skip_bot_messages = OptionsInfo[bool](
        description="Don't show previous messages sent by this bot.",
        default_value=True,
        parser=_parse_bool,
    )
    send_meta_event = OptionsInfo[bool](
        description="Send meta events to the Poe server.",
        default_value=True,
        parser=_parse_bool,
    )
    content_type = OptionsInfo[ContentType](
        description="Send meta events to the Poe server.",
        default_value="text/markdown",
        parser=_parse_content_type,
    )
    linkify = OptionsInfo[bool](
        description="Meta event response field for whether Poe should linkify the response.",
        default_value=True,
        parser=_parse_bool,
    )
    refetch_settings = OptionsInfo[bool](
        description="Meta event response field for whether Poe should resend `settings` requests.",
        default_value=True,
        parser=_parse_bool,
    )
    context_clear_window_secs = OptionsInfo[Optional[int]](
        description=(
            "Poe settings response field which represents the amount of time "
            "in seconds Poe waits before clearing the context and restarting "
            "the conversation. If None, Poe decides how long to wait before "
            "clearing the context. If 0, the window is never cleared."
        ),
        default_value=0,
        parser=_parse_optional_nonnegative_int,
    )
    allow_user_context_clear = OptionsInfo[bool](
        description=(
            "Poe settings response field for whether the user should be "
            "allowed to directly clear the conversation."
        ),
        default_value=True,
        parser=_parse_bool,
    )
    suggested_reply = OptionsInfo[list[str]](
        description=(
            "The list of reply text this bot should suggest. You can assign "
            "to this field using 3 different syntaxes:\n"
            "- JSON list form: replace the existing suggested_reply list with "
            'the given list. Example: suggested_reply=["reply 1", "reply 2"]\n'
            '- +"<reply>": append the given reply to the list. Example: '
            'suggested_reply=+"new reply"\n'
            '- -"<reply>": remove the given reply to the list. Example: '
            'suggested_reply=-"existing reply"\n'
        ),
        default_value=list(cmd for cmd in ALL_COMMANDS if " " not in cmd),
        parser=_parse_list_str,
    )


class DebugBot(PoeBot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests: list[QueryRequest] = []
        self.commands = {
            "commands": self._list_commands,
            "assign": self._handle_option_assignment,
            "options": self._list_options,
            "reset": self._reset,
            "error": self._error,
        }
        self.options = {
            opt.name: opt.value.default_value
            for opt in BotOptions
        }
        self.error_message_counts = Counter()

    async def get_response(
        self, query: QueryRequest, request: web.Request
    ) -> AsyncIterator[Event]:
        """Return an async iterator of events to send to the user."""
        if self._get_option(BotOptions.send_meta_event):
            yield self.meta_event(
                content_type=self._get_option(BotOptions.content_type),
                linkify=self._get_option(BotOptions.linkify),
                refetch_settings=self._get_option(BotOptions.refetch_settings),
            )
        query_data = copy.deepcopy(query)
        last_message = query_data["query"][-1]["content"].strip()
        cmd, *args = last_message.split(None, 1)
        if (handler := self.commands.get(cmd.lower())) is not None:
            async for ev in handler(query_data, *args):
                yield ev
        else:
            yield self.text_event(f"{last_message}\n")

        if self._get_option(BotOptions.skip_bot_messages):
            query_data["query"] = [q for q in query_data["query"] if q["role"] != "bot"]

        if (n_prev := self._get_option(BotOptions.n_previous_messages)) is not None:
            query_data["query"] = query_data["query"][-n_prev:]

        self.requests.append(query_data)
        request_str = json.dumps(
            self.requests[0]
            if len(self.requests) == 1
            else self.requests,
            indent=4,
        )
        self.requests = []
        yield self.text_event(
            f"```\n{request_str}\n```"
            if self._get_option(BotOptions.content_type) == "text/markdown"
            else request_str
        )

        for reply in self._get_option(BotOptions.suggested_reply):
            yield self.suggested_reply_event(reply)

    async def on_feedback(self, feedback: ReportFeedbackRequest) -> None:
        """Called when we receive user feedback such as likes."""
        self.requests.append(copy.deepcopy(feedback))
        print(
            f"User {feedback['user_id']} gave feedback on {feedback['conversation_id']}"
            f"message {feedback['message_id']}: {feedback['feedback_type']}"
        )

    async def on_error(self, error: ReportErrorRequest) -> None:
        super().on_error(error)
        self.requests.append(copy.deepcopy(error))

    async def get_settings(self, request: SettingsRequest) -> SettingsResponse:
        """Return the settings for this bot."""
        self.requests.append(copy.deepcopy(request))
        return {
            "context_clear_window_secs": self._get_option(BotOptions.context_clear_window_secs),
            "allow_user_context_clear": self._get_option(BotOptions.allow_user_context_clear),
        }

    def _get_option(self, opt: BotOptions) -> Any:
        return self.options[opt.name]

    def _set_option(self, key: str, value: str) -> None:
        old_value = self.options[key]
        new_value = BotOptions[key].value.parser(old_value, value)
        self.options[key] = new_value

    async def _handle_option_assignment(self, query: QueryRequest, *args) -> AsyncIterator[Event]:
        if len(args) != 1:
            yield self.text_event(
                f"`assign` requires an argument of the form <option_name>=<value>."
            )
            return
        lvalue, assignment, rvalue = args[0].partition("=")
        if assignment != "=":
            yield self.text_event(
                f"`assign` requires an argument of the form <option_name>=<value>."
            )
            return

        key = lvalue.lower().strip()
        if key not in self.options:
            yield self.text_event(f"Option '{key}' does not exist!\n")
        else:
            try:
                self._set_option(key, rvalue)
                yield self.text_event(f"Set option {key}={self.options[key]}.\n")
            except Exception as e:
                yield self.text_event(
                    f"Could not set option {key} due to error {repr(e)}.\n"
                )

    async def _list_options(self, query: QueryRequest, *args) -> AsyncIterator[Event]:
        if len(args) != 0:
            yield self.text_event(
                f"WARNING: `options` does not take arguments, but got '{args}'."
            )
        for key, value in self.options.items():
            yield self.text_event(f"{key}={value}\n")

    async def _list_commands(self, query: QueryRequest, *args) -> AsyncIterator[Event]:
        if len(args) != 0:
            yield self.text_event(
                f"WARNING: `command` does not take arguments, but got '{args}'."
            )
        for command, description in ALL_COMMANDS.items():
            yield self.text_event(f"{command}: {description}\n")

    async def _reset(self, query: QueryRequest, *args) -> AsyncIterator[Event]:
        if len(args) != 0:
            yield self.text_event(
                f"WARNING: `command` does not take arguments, but got '{args}'."
            )
        self.options = {
            opt.name: opt.value.default_value
            for opt in BotOptions
        }
        async for ev in self._list_options(query):
            yield ev

    async def _error(self, query: QueryRequest, *args) -> AsyncIterator[Event]:
        if len(args) != 1:
            yield self.text_event(
                f"WARNING: `error` expects at least one argument."
            )
            return
        arg_str = args[0]
        retries, *cmd_args = arg_str.split(None, 1)
        try:
            retries = _parse_optional_nonnegative_int(0, retries) or 0
            message = json.loads(cmd_args[0])
        except Exception as e:
            retries = 0
            try:
                message = json.loads(arg_str)
            except Exception as e:
                yield self.text_event(
                    "Could not parse `error` arguments. It needs to be in the "
                    "form `error [retries] <message>"
                )
                return

        if arg_str in self.error_message_counts:
            self.error_message_counts[arg_str] -= 1
        else:
            self.error_message_counts[arg_str] = retries

        yield self.error_event(
            message, allow_retry=self.error_message_counts[arg_str] > 0
        )


if __name__ == "__main__":
    run(DebugBot())
