from typing import Any, Literal, Tuple

from boto3 import Session
from langchain_aws import BedrockEmbeddings, InMemoryVectorStore, ChatBedrockConverse
from langchain_core.messages import BaseMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import logging
from languagemodel import LanguageModel
from retriever import Retriever
from langgraph.graph import StateGraph, END
from langgraph.types import Command
from langchain_core.messages.human import HumanMessage
from typing_extensions import List, TypedDict
import textwrap
import json

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def messages_to_history_str(messages: list[BaseMessage]) -> str:
    """Convert messages to a history string."""
    string_messages = []
    for message in messages:
        role = message.type
        content = message.content
        string_message = f"{role}: {content}"

        additional_kwargs = message.additional_kwargs
        if additional_kwargs:
            string_message += f"\n{additional_kwargs}"
        string_messages.append(string_message)
    return "\n".join(string_messages)


class Prompts:
    def __init__(self, jsonfile: str):
        with open(jsonfile, 'r') as file:
            data = json.load(file)
            for k, v in data.items():
                self.__setattr__(k, self.__parse__(v))

    def __parse__(self, prompt_dicts: List[dict]):
        return ChatPromptTemplate([(d["role"], d["content"]) for d in prompt_dicts])


# Define state for application
class State(TypedDict):
    question: str # the user question
    history: List[BaseMessage] # all the interactions between ai and user
    context: dict # retrieved documents to use as context source
    additional_context: str = "" # additional info added by the user to be considered a valid source
    query_aug: bool # use or not query augmentation technique before passing the question to the retriever
    input_tokens_count: int # amount of input tokens processed by the whole chain of llm calls triggered in this round
    output_tokens_count: int # amount of output tokens processed by the whole chain of llm calls triggered in this round
    answer: str # textual answer generated by the system and returned to the user


class Rag:
    NORETRIEVE_MSG = "Mi dispiace, non sono riuscito a trovare informazioni rilevanti nelle linee guida."
    NOTALLOWED_MSG = "Mi dispiace, non posso rispondere a questa domanda."

    def __init__(self, session: Session,
                 model: ChatBedrockConverse | str,
                 embedder: BedrockEmbeddings | str,
                 vector_store: InMemoryVectorStore | str | None = None,
                 **kwargs):
        self.prompts = Prompts(kwargs.get("promptfile", "./prompts.json"))
        self.session = session
        client = session.client("bedrock-runtime", region_name=kwargs.get("region"))
        self.llm = LanguageModel(model, client=client, model_low=kwargs.get("model_low", None),
                                 model_pro=kwargs.get("model_pro", None))
        self.retriever = Retriever(embedder, vector_store=vector_store, client=client)
        graph_builder = StateGraph(State)
        graph_builder.set_entry_point("orchestrator")
        graph_builder.add_node("orchestrator", self.orchestrator)
        graph_builder.add_node("history_consolidator", self.history_consolidator)
        graph_builder.add_node("augmentator", self.augmentator)
        graph_builder.add_node("doc_retriever", self.doc_retriever)
        graph_builder.add_node("generator", self.generator)
        self.graph = graph_builder.compile()

    def generate_norag(self, input: str):
        messages = self.prompts.question_open.invoke({"question": input}).messages
        response = self.llm.generate(messages=messages)
        return {"answer": response.content,
                "input_tokens_count": response.usage_metadata["input_tokens"],
                "output_tokens_count": response.usage_metadata["output_tokens"]}

    def orchestrator(self, state: State) -> Command[Literal["augmentator", "doc_retriever", "history_consolidator"]]:
        logger.debug(f"Dispatching request: {state}")
        previous_user_interactions = [message for message in state["history"] if type(message) is HumanMessage]
        if len(previous_user_interactions) > 0:
            return Command(goto="history_consolidator")
        else:
            return Command(goto="augmentator" if state["query_aug"] else "doc_retriever")

    def doc_retriever(self, state: State) -> Command[Literal["generator", END]]:
        logger.debug(f"New retrieval: {state}")
        retrieved_docs, scores = self.retriever.retrieve_with_scores(state["question"], n=10, score_threshold=0.6)
        logger.debug(f"Retrieved docs: {retrieved_docs}")
        additional_context = state.get("additional_context", None)
        if len(retrieved_docs) == 0 and (type(additional_context) is not str or additional_context == ""):
            return Command(
                update={"context": {"docs": retrieved_docs, "scores": scores},
                        "answer": self.NORETRIEVE_MSG},
                goto=END,
            )
        else:
            return Command(
                update={"context": {"docs": retrieved_docs, "scores": scores}},
                goto="generator",
            )

    def history_consolidator(self, state: State) -> Command[Literal["orchestrator"]]:
        logger.debug(f"Consolidating previous history...")
        if len(state["history"]) > 5:
            proximal_history = state["history"][-5:]
        else:
            proximal_history = state["history"]
        messages = self.prompts.history_consolidation.invoke({"question": state["question"],
                                                              "history": messages_to_history_str(
                                                                  state["history"])}).messages
        logger.debug(messages)
        logger.debug(proximal_history)
        response = self.llm.generate(messages=messages)
        consolidated_question = response.content
        logger.debug(f"Consolidated query: {textwrap.shorten(consolidated_question, width=30)}")
        return Command(
            update={"question": consolidated_question,
                    "history": [],
                    "input_tokens_count": state["input_tokens_count"] + response.usage_metadata["input_tokens"],
                    "output_tokens_count": state["output_tokens_count"] + response.usage_metadata["output_tokens"]},
            goto="orchestrator",
        )

    def augmentator(self, state: State) -> Command[Literal["doc_retriever"]]:
        logger.debug(f"Expanding query...")
        messages = self.prompts.query_expansion.invoke({"question": state["question"]}).messages
        response = self.llm.generate(messages=messages)
        augmented_question = response.content
        logger.debug(f"Expanded query: {textwrap.shorten(augmented_question, width=30)}")
        return Command(
            update={"question": augmented_question,
                    "input_tokens_count": state["input_tokens_count"] + response.usage_metadata["input_tokens"],
                    "output_tokens_count": state["output_tokens_count"] + response.usage_metadata["output_tokens"]
                    },
            goto="doc_retriever",
        )

    def generator(self, state: State) -> Command[Literal[END]]:
        doc_strings = []
        for i, doc in enumerate(state["context"]["docs"]):
            doc_strings.append(f"Source {i+1}:\n{doc.page_content}")
        additional_context = state.get("additional_context", None)
        if type(additional_context) is str and additional_context != "":
            logger.debug(f"Appending additional context...")
            doc_strings.append(f"Source [0]:\n{additional_context}")
        docs_content = "\n".join(doc_strings)
        messages = self.prompts.question_with_context_inline_cit.invoke({"question": state["question"], "context": docs_content}).messages
        response = self.llm.generate(messages=messages, level="pro")
        return Command(update={"answer": response.content,
                               "input_tokens_count": state["input_tokens_count"] + response.usage_metadata["input_tokens"],
                               "output_tokens_count": state["output_tokens_count"] + response.usage_metadata["output_tokens"]},
                       goto=END)

    def invoke(self, input: dict[str, Any]):
        return self.graph.invoke(input)

    def get_image(self):
        return self.graph.get_graph().draw_mermaid_png()
