import json
import re
from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Optional

from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

from approaches.approach import Approach


class ChatApproach(Approach, ABC):
    follow_up_questions_prompt_content = """Genereer 3 zeer korte vervolgvraagstellingen die de gebruiker waarschijnlijk als volgende zou stellen.
    Sluit de vervolgvraagstellingen in dubbele hoekhaken. Voorbeeld:
    <<Zijn er uitzonderingen voor subsidies?>>
    <<Welke stappen zijn nodig voor een vergunningaanvraag?>>
    <<Hoe lang duurt het om een besluit te krijgen?>>
    Herhaal geen vragen die al gesteld zijn.
    Zorg ervoor dat de laatste vraag eindigt met ">>".
    """

    @property
    @abstractmethod
    def system_message_chat_conversation(self) -> str:
        pass

    @abstractmethod
    async def run_until_final_call(self, messages, overrides, auth_claims, should_stream) -> tuple:
        pass

    def get_system_prompt(
        self, override_prompt: Optional[str], follow_up_questions_prompt: str, sources_reference_content: str = ""
    ) -> str:
        if override_prompt is None:
            return self.system_message_chat_conversation.format(
                injected_prompt="",
                follow_up_questions_prompt=follow_up_questions_prompt,
                sources_reference_content=sources_reference_content,
            )
        elif override_prompt.startswith(">>>"):
            return self.system_message_chat_conversation.format(
                injected_prompt=override_prompt[3:] + "\n",
                follow_up_questions_prompt=follow_up_questions_prompt,
                sources_reference_content=sources_reference_content,
            )
        else:
            return override_prompt.format(
                follow_up_questions_prompt=follow_up_questions_prompt,
                sources_reference_content=sources_reference_content,
            )

    def extract_followup_questions(self, content: Optional[str]):
        if content is None:
            return content, []
        return content.split("<<")[0], re.findall(r"<<([^>>]+)>>", content)

    async def run_without_streaming(
        self,
        messages: list[ChatCompletionMessageParam],
        overrides: dict[str, Any],
        auth_claims: dict[str, Any],
        session_state: Any = None,
    ) -> dict[str, Any]:
        extra_info, chat_coroutine = await self.run_until_final_call(
            messages, overrides, auth_claims, should_stream=False
        )
        chat_completion_response: ChatCompletion = await chat_coroutine
        content = chat_completion_response.choices[0].message.content
        role = chat_completion_response.choices[0].message.role
        if overrides.get("suggest_followup_questions"):
            content, followup_questions = self.extract_followup_questions(content)
            extra_info["followup_questions"] = followup_questions
        chat_app_response = {
            "message": {"content": content, "role": role},
            "context": extra_info,
            "session_state": session_state,
        }
        return chat_app_response

    async def run_with_streaming(
        self,
        messages: list[ChatCompletionMessageParam],
        overrides: dict[str, Any],
        auth_claims: dict[str, Any],
        session_state: Any = None,
    ) -> AsyncGenerator[dict, None]:
        extra_info, chat_coroutine = await self.run_until_final_call(
            messages, overrides, auth_claims, should_stream=True
        )
        yield {"delta": {"role": "assistant"}, "context": extra_info, "session_state": session_state}

        followup_questions_started = False
        followup_content = ""
        async for event_chunk in await chat_coroutine:
            # "2023-07-01-preview" API version has a bug where first response has empty choices
            event = event_chunk.model_dump()  # Convert pydantic model to dict
            if event["choices"]:
                completion = {
                    "delta": {
                        "content": event["choices"][0]["delta"].get("content"),
                        "role": event["choices"][0]["delta"]["role"],
                    }
                }
                # if event contains << and not >>, it is start of follow-up question, truncate
                content = completion["delta"].get("content")
                content = content or ""  # content may either not exist in delta, or explicitly be None
                if overrides.get("suggest_followup_questions") and "<<" in content:
                    followup_questions_started = True
                    earlier_content = content[: content.index("<<")]
                    if earlier_content:
                        completion["delta"]["content"] = earlier_content
                        yield completion
                    followup_content += content[content.index("<<") :]
                elif followup_questions_started:
                    followup_content += content
                else:
                    yield completion
        if followup_content:
            _, followup_questions = self.extract_followup_questions(followup_content)
            yield {"delta": {"role": "assistant"}, "context": {"followup_questions": followup_questions}}

    async def run(
        self,
        messages: list[ChatCompletionMessageParam],
        session_state: Any = None,
        context: dict[str, Any] = {},
    ) -> dict[str, Any]:
        overrides = context.get("overrides", {})
        auth_claims = context.get("auth_claims", {})
        return await self.run_without_streaming(messages, overrides, auth_claims, session_state)

    async def run_stream(
        self,
        messages: list[ChatCompletionMessageParam],
        session_state: Any = None,
        context: dict[str, Any] = {},
    ) -> AsyncGenerator[dict[str, Any], None]:
        overrides = context.get("overrides", {})
        auth_claims = context.get("auth_claims", {})
        return self.run_with_streaming(messages, overrides, auth_claims, session_state)
