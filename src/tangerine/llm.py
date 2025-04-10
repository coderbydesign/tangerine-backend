import logging
import time
from typing import Generator

from langchain_community.callbacks.manager import get_openai_callback
from langchain_community.callbacks.openai_info import OpenAICallbackHandler
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

import tangerine.config as cfg
from tangerine.metrics import get_counter, get_gauge
from tangerine.models.assistant import Assistant

log = logging.getLogger("tangerine.llm")

assistant_response_counter = get_counter(
    "assistant_response_counter",
    "Total number of responses for an assistant",
    ["assistant_id", "assistant_name"],
)
llm_completion_tokens_metric = get_counter("llm_completion_tokens", "LLM completion tokens usage")
llm_prompt_tokens_metric = get_counter("llm_prompt_tokens", "LLM prompt tokens usage")
llm_completion_rate = get_gauge(
    "llm_completion_rate", "Observed tokens per sec from most recent LLM chat completion"
)
llm_processing_rate = get_gauge(
    "llm_processing_rate", "Observed tokens per sec for most recent LLM processing after prompted"
)
llm_no_answer = get_counter(
    "llm_no_answer", "No search results found", ["assistant_id", "assistant_name"]
)


def _record_metrics(
    cb: OpenAICallbackHandler,
    processing_start: float,
    completion_start: float,
    completion_end: float,
) -> None:
    if not completion_start:
        log.error("no content in llm response stream")
        return

    processing_time = completion_start - processing_start
    completion_time = completion_end - completion_start

    try:
        processing_rate = cb.prompt_tokens / processing_time
        completion_rate = cb.completion_tokens / completion_time
    except ZeroDivisionError:
        log.error("unexpected time diff of 0")
        completion_rate = 0

    log.debug(
        (
            "prompt tokens: %s, completion tokens: %s, "
            "processing time: %fsec (%f tokens/sec), completion time: %fsec (%f tokens/sec)"
        ),
        cb.prompt_tokens,
        cb.completion_tokens,
        processing_time,
        processing_rate,
        completion_time,
        completion_rate,
    )
    llm_completion_tokens_metric.inc(cb.completion_tokens)
    llm_prompt_tokens_metric.inc(cb.prompt_tokens)
    llm_processing_rate.set(processing_rate)
    llm_completion_rate.set(completion_rate)


def _build_context(search_results: list[Document], content_char_limit: int = 0):
    search_metadata = []
    context = ""
    log.debug("given %d search results as context", len(search_results))
    for i, doc in enumerate(search_results):
        page_content = doc.document.page_content
        metadata = doc.document.metadata
        search_metadata.append(
            {
                "metadata": metadata,
                "page_content": page_content,
            }
        )

        context += f"\n<<Search result {i + 1}"
        if "title" in metadata:
            title = metadata["title"]
            context += f", document title: '{title}'"
        limit = content_char_limit if content_char_limit else len(page_content)
        search_result = page_content[0:limit]
        context += f">>\n\n{search_result}\n\n<<Search result {i + 1} END>>\n"

    return context, search_metadata


def _get_response(
    prompt: ChatPromptTemplate,
    prompt_params: dict,
) -> Generator[str, None, None]:
    chat = ChatOpenAI(
        model=cfg.LLM_MODEL_NAME,
        openai_api_base=cfg.LLM_BASE_URL,
        openai_api_key=cfg.LLM_API_KEY,
        temperature=cfg.LLM_TEMPERATURE,
        stream_usage=True,
    )

    chain = prompt | chat

    completion_start = 0.0
    processing_start = time.time()

    with get_openai_callback() as cb:
        for chunk in chain.stream(prompt_params):
            if not completion_start:
                # this is the first output token received
                completion_start = time.time()
            if len(chunk.content):
                yield chunk.content

            # end for
            completion_end = time.time()

        # end with
    _record_metrics(cb, processing_start, completion_start, completion_end)


def rerank(query, search_results):
    log.debug("llm 'rerank' request")
    if len(search_results) <= 1:
        return search_results  # No need to rank if there's only one result

    context, _ = _build_context(search_results, 300)

    prompt = ChatPromptTemplate(
        [("system", cfg.RERANK_SYSTEM_PROMPT), ("user", cfg.RERANK_PROMPT_TEMPLATE)]
    )
    prompt_params = {"query": query, "context": context}

    llm_response, _ = _get_response(prompt, prompt_params)

    return "".join(llm_response)


def ask(
    assistant: Assistant,
    previous_messages,
    question,
    search_results: list[Document],
    interaction_id=None,
) -> tuple[Generator[str, None, None], list[dict]]:
    log.debug("llm 'ask' request")
    search_context = ""
    search_metadata = []

    if len(search_results) == 0:
        log.debug("given 0 search results")
        search_context = "No matching search results found"
        llm_no_answer.labels(assistant_id=assistant.id, assistant_name=assistant.name).inc()
    else:
        search_context, search_metadata = _build_context(search_results)
        assistant_response_counter.labels(
            assistant_id=assistant.id, assistant_name=assistant.name
        ).inc()

    if not search_metadata:
        search_metadata = [{}]
    for m in search_metadata:
        m["interactionId"] = interaction_id

    msg_list = [("system", assistant.system_prompt or cfg.DEFAULT_SYSTEM_PROMPT)]
    if previous_messages:
        for msg in previous_messages:
            if msg["sender"] == "human":
                msg_list.append(("human", f"[INST] {msg['text']} [/INST]"))
            if msg["sender"] == "ai":
                msg_list.append(("ai", f"{msg['text']}</s>"))
    msg_list.append(("human", cfg.USER_PROMPT_TEMPLATE))

    prompt_params = {"context": search_context, "question": question}
    llm_response = _get_response(ChatPromptTemplate(msg_list), prompt_params)

    return llm_response, search_metadata
