import json
from pathlib import Path
import csv
import boto3
import gradio as gr
import os
import logging

import numpy as np
from rags import Rag
from dotenv import dotenv_values
import yaml
import random
from boto3 import Session
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import textwrap
import re
import io
from PIL import Image
import argparse
from datetime import datetime, timedelta
from collections import defaultdict
from gradio_modal import Modal
import dayplot as dp

# Define the parser
parser = argparse.ArgumentParser()
parser.add_argument('--settings', action="store", dest='settings_file', default='settings.yaml')
parser.add_argument('--sslcert', action="store", dest='ssl_certfile', default=None)
parser.add_argument('--sslkey', action="store", dest='ssl_keyfile', default=None)
parser.add_argument('--debug', action="store", dest='debug', default=False, type=bool)
parser.add_argument('--local', action="store", dest='local', default=False, type=bool)
args = parser.parse_args()


# GLOBAL VARIABLES: SHARED BETWEEN USERS AND SESSIONS
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

with open(args.settings_file) as stream:
    config = yaml.safe_load(stream)

wd = os.path.abspath(os.path.dirname(args.settings_file))
os.chdir(wd)


AWS_SECRETS = config.get("bedrock").get("secrets-path")
GRADIO_SECRETS = config.get("gradio").get("secrets-path")

logger.debug("Initializing RAG...")
rag = Rag(session=Session(),
          model=config.get("bedrock").get("models").get("model-id"),
          embedder=config.get("bedrock").get("embedder-id"),
          vector_store=config.get("vector-db-path"),
          region=config.get("bedrock").get("region"),
          model_pro=config.get("bedrock").get("models").get("pro-model-id"),
          model_low=config.get("bedrock").get("models").get("low-model-id"))

def get_mfa_response(mfa_token, duration: int = 900):
    logger.debug("Checking MFA token...")
    if len(mfa_token) != 6:
        return None
    try:
        sts_client = boto3.client('sts',
                            aws_access_key_id=dotenv_values(AWS_SECRETS).get("AWS_ACCESS_KEY_ID"),
                            aws_secret_access_key=dotenv_values(AWS_SECRETS).get("AWS_SECRET_ACCESS_KEY"))
        response = sts_client.get_session_token(DurationSeconds=duration,
                                                SerialNumber=dotenv_values(AWS_SECRETS).get("AWS_ARN_MFA_DEVICE"),
                                                TokenCode=mfa_token)
        return response
    except Exception as e:
        logger.error(str(e))
        return None


def token_auth(username: str, password: str):
    logger.info(f"Login attempt from user '{username}'")
    # ADMIN LOGIN
    if username == dotenv_values(GRADIO_SECRETS).get("GRADIO_ADMNUSR"):
        return get_mfa_response(str(password)) is not None
    # OTHER USERS LOGIN
    else:
        for user,pwd in zip(json.loads(dotenv_values(GRADIO_SECRETS).get("GRADIO_USRS")), json.loads(dotenv_values(GRADIO_SECRETS).get("GRADIO_PWDS"))):
            check_user = (username == user)
            check_password = (password == pwd)
            if check_user and check_password:
                return True
        return False
    return False


def update_rag(mfa_token, use_mfa_session=args.local):
    global rag
    logger.debug("Trying to update rag...")
    mfa_response = get_mfa_response(mfa_token)
    if mfa_response is not None:
        try:
            if use_mfa_session:
                session = boto3.Session(aws_access_key_id=mfa_response['Credentials']['AccessKeyId'],
                                        aws_secret_access_key=mfa_response['Credentials']['SecretAccessKey'],
                                        aws_session_token=mfa_response['Credentials']['SessionToken'])
            else:
                session = Session()
            rag_attempt = Rag(session=session,
                            model=config.get("bedrock").get("models").get("model-id"),
                            embedder=config.get("bedrock").get("embedder-id"),
                            vector_store=config.get("vector-db-path"),
                            region=config.get("bedrock").get("region"),
                            model_pro=config.get("bedrock").get("models").get("pro-model-id"),
                            model_low=config.get("bedrock").get("models").get("low-model-id"))
            rag = rag_attempt
            logger.debug("Rag updated")
            return True, ""
        except Exception as e:
            logger.error("update failed")
            logger.error(str(e))
            return False, ""
    else:
        return False, ""


def upload_file(filepath: str):
    rag.retriever.upload_file(filepath)

def from_list_to_messages(chat:list[dict]):
    template = ChatPromptTemplate([MessagesPlaceholder("history")]).invoke({"history":[(message["role"],message["content"]) for message in chat]})
    return template.to_messages()

LOG_STAT_FILE = "logs/token_usage.json"

def log_token_usage(ip_address: str, input_tokens: int, output_tokens: int):
    """Logs the input and output token usage for a given IP address with timestamps."""
    os.makedirs(os.path.dirname(LOG_STAT_FILE), exist_ok=True)

    if os.path.exists(LOG_STAT_FILE):
        with open(LOG_STAT_FILE, "r") as file:
            data = json.load(file)
    else:
        data = {}

    # Ensure IP has an entry
    if ip_address not in data:
        data[ip_address] = {"input_tokens": [], "output_tokens": []}

    # Append new token usage
    now = datetime.now().isoformat()
    data[ip_address]["input_tokens"].append((input_tokens, now))
    data[ip_address]["output_tokens"].append((output_tokens, now))

    # Save back to file
    with open(LOG_STAT_FILE, "w") as file:
        json.dump(data, file, indent=4)

def get_usage_stats():
    """Computes total users, total input/output tokens, averages, and cumulative daily token usage."""
    if not os.path.exists(LOG_STAT_FILE):
        return {
            "total_users": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "avg_input_tokens_per_user_per_day": 0,
            "avg_output_tokens_per_user_per_day": 0,
            "cumulative_tokens_per_day": [],
            "cumulative_input_tokens_per_day": [],
            "cumulative_output_tokens_per_day": []
        }

    with open(LOG_STAT_FILE, "r") as file:
        data = json.load(file)

    total_users = len(data)
    total_input_tokens = 0
    total_output_tokens = 0
    daily_totals = defaultdict(lambda: [0, 0])  # {date: [input_tokens, output_tokens]}

    for usage in data.values():
        for tokens, timestamp in usage["input_tokens"]:
            date = datetime.fromisoformat(timestamp).date()
            total_input_tokens += tokens
            daily_totals[date][0] += tokens

        for tokens, timestamp in usage["output_tokens"]:
            date = datetime.fromisoformat(timestamp).date()
            total_output_tokens += tokens
            daily_totals[date][1] += tokens

    # Compute averages
    active_days = len(daily_totals)
    avg_input_tokens_per_user_per_day = total_input_tokens / (total_users * active_days) if total_users > 0 and active_days > 0 else 0
    avg_output_tokens_per_user_per_day = total_output_tokens / (total_users * active_days) if total_users > 0 and active_days > 0 else 0

    # Cumulative token count per day
    sorted_dates = sorted(daily_totals.keys())
    cumulative_input = 0
    cumulative_output = 0
    cumulative_tokens_per_day = []
    cumulative_input_tokens_per_day = []
    cumulative_output_tokens_per_day = []

    for date in sorted_dates:
        cumulative_input += daily_totals[date][0]
        cumulative_output += daily_totals[date][1]
        cumulative_tokens_per_day.append((cumulative_input + cumulative_output, date.isoformat()))
        cumulative_input_tokens_per_day.append((cumulative_input, date.isoformat()))
        cumulative_output_tokens_per_day.append((cumulative_output, date.isoformat()))

    return {
        "total_users": total_users,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "avg_input_tokens_per_user_per_day": round(avg_input_tokens_per_user_per_day),
        "avg_output_tokens_per_user_per_day": round(avg_output_tokens_per_user_per_day),
        "cumulative_tokens_per_day": cumulative_tokens_per_day,
        "cumulative_input_tokens_per_day": cumulative_input_tokens_per_day,
        "cumulative_output_tokens_per_day": cumulative_output_tokens_per_day,
        "daily_totals": daily_totals,
    }

import plotly.graph_objects as go

def plot_cumulative_tokens():
    """Plots cumulative token usage over time with stacked bars for input and output tokens and a line for total cumulative tokens using Plotly."""
    stats = get_usage_stats()
    if not stats["cumulative_tokens_per_day"]:
        logger.warning("No data to plot.")
        return

    dates = [datetime.fromisoformat(d) for _, d in stats["cumulative_tokens_per_day"]]
    input_tokens = [t for t, _ in stats["cumulative_input_tokens_per_day"]]
    output_tokens = [t for t, _ in stats["cumulative_output_tokens_per_day"]]
    total_tokens = [t for t, _ in stats["cumulative_tokens_per_day"]]

    fig = go.Figure()

    fig.add_trace(go.Bar(x=dates, y=input_tokens, name="Input Tokens", marker_color='royalblue',width=1000*3600*24*0.5))
    fig.add_trace(go.Bar(x=dates, y=output_tokens, name="Output Tokens", marker_color='lightsalmon',width=1000*3600*24*0.5))
    fig.add_trace(go.Scatter(x=dates, y=total_tokens, mode='lines+markers', name="Total", line=dict(color='darkslategrey')))

    fig.update_layout(
        title="Cumulative Token Usage Over Time",
        xaxis_title="Date",
        yaxis_title="Tokens [tokens/dd]",
        barmode='stack',
        xaxis=dict(tickangle=-45),
        legend_title="Legend",
        template="plotly_white"
    )

    return fig

import matplotlib.pyplot as plt
def plot_daily_tokens_heatmap():
    """Plots cumulative token usage over time with stacked bars for input and output tokens and a line for total cumulative tokens using Plotly."""
    stats = get_usage_stats()
    if not stats["daily_totals"]:
        logger.warning("No data to plot.")
        return

    dates = stats["daily_totals"].keys()
    total_tokens = [t[0]+t[1] for _, t in stats["daily_totals"].items()]
    fig, ax = plt.subplots(figsize=(15, 6))
    dp.calendar(
        dates,
        total_tokens,
        start_date=datetime.now() - timedelta(days=365),
        end_date=datetime.now(),
        ax=ax,
    )
    fig.tight_layout()

    return fig


LOG_FILE = "logs/usage_log.json"

#20K = approx 20 cents with most expensive models
def check_ban(ip_address: str, max_tokens: int = 20000) -> bool:
    tokens_consumed, last_access, banned = read_usage_log(ip_address)
    timediff = datetime.now() - last_access
    if tokens_consumed>max_tokens:
        # if you exceeded the tokens quota and less than 24 hours have passed since last call
        # then you are banned
        if timediff.total_seconds()<(24*60*60):
            update_usage_log(ip_address, 0, True)
            return True
        # if you exceeded the tokens quota but your last call was more than 24 hours ago, the ban ends
        else:
            update_usage_log(ip_address, 0, False)
            return False
    # if you did not exceed the tokens quota, you are always good to go
    else:
        return False

def read_usage_log(ip_address: str) -> (int, datetime, bool):
    if not os.path.exists(LOG_FILE):
        return 0, datetime.now(), False
    with open(LOG_FILE, "r") as file:
        data = json.load(file)
    if ip_address in data:
        entry = data[ip_address]
        return entry["tokens_count"], datetime.fromisoformat(entry["last_call"]), entry["banned_flag"]
    return 0, datetime.now(), False


def update_usage_log(ip_address: str, tokens_consumed: int, banned: bool):
    """Updates the usage log, modifying the entry for the given IP address."""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as file:
            data = json.load(file)
    else:
        data = {}

    was_banned = data.get(ip_address, {}).get("banned_flag", False)
    tokens_count = data.get(ip_address, {}).get("tokens_count", 0) + tokens_consumed

    if was_banned and not banned:
        tokens_count = 0  # Reset tokens if ban is lifted

    data[ip_address] = {
        "tokens_count": tokens_count,
        "last_call": datetime.now().isoformat(),
        "banned_flag": banned
    }

    with open(LOG_FILE, "w") as file:
        json.dump(data, file, indent=4)

def dot_progress_bar(score, total_dots=7):
    filled_count = round(score * total_dots)
    empty_count = total_dots - filled_count
    filled = "•" * filled_count
    empty = "·" * empty_count
    return f"{filled}{empty} {round(score*100,2)}%"

def reply(message, history, is_admin, enable_rag, additional_context, query_aug, request: gr.Request):
    if rag is None:
        logger.error("LLM not configured")
        gr.Error("Error: LLM not configured")
    else:
        is_banned = check_ban(request.client.host) if not is_admin else False
        if is_banned:
            logger.error("exceeded daily usage limit!")
            gr.Error("Error: exceeded daily usage limit")
            return [gr.ChatMessage(role="assistant", content="Sembra che tu abbia esaurito la tua quota giornaliera. Riprova più tardi.")]
        try:
            if enable_rag:
                response = rag.invoke({"question": message,
                                       "history": from_list_to_messages(history),
                                       "additional_context": additional_context,
                                       "input_tokens_count":0,
                                       "output_tokens_count":0,
                                       "query_aug": query_aug})
                answer = response["answer"]
                input_tokens_count = response["input_tokens_count"]
                output_tokens_count = response["output_tokens_count"]
                update_usage_log(request.client.host, input_tokens_count+output_tokens_count*4, False)
                log_token_usage(request.client.host, input_tokens_count, output_tokens_count)
                answer = re.sub(r"(\[[\d,\s]*\])",r"<sup>\1</sup>",answer)
                citations = {}
                citations_str = ""
                retrieved_documents = response["context"]["docs"]
                retrieved_scores = response["context"]["scores"]
                for i, document in enumerate(retrieved_documents):
                    source = os.path.basename(document.metadata.get("source", ""))
                    content = document.page_content
                    doc_string = f"[{i}] **{source}** - *\"{textwrap.shorten(content,500)}\"* (Confidenza: {dot_progress_bar(retrieved_scores[i])})"
                    citations.update({i: {"source":source, "content":content}})
                    citations_str += ("- "+doc_string+"\n")
                return [gr.ChatMessage(role="assistant", content=answer),
                        gr.ChatMessage(role="assistant", content=citations_str,
                                    metadata={"title": "📖 Linee guida correlate"})]
            else:
                response = rag.generate_norag(message)
                answer = response["answer"]
                return gr.ChatMessage(role="assistant", content=answer)
        except Exception as e:
            logger.error(str(e))
            gr.Error("Error: " + str(e))


def onload(disclaimer_seen:bool, request: gr.Request):
    logging_info = {
        "username":request.username,
        "ip":request.client.host,
        "headers":request.headers,
        "session_hash":request.session_hash,
        "query_params":dict(request.query_params)
    }
    logger.debug(f"Login details: {logging_info}")
    admin_priviledges = request.username == dotenv_values(GRADIO_SECRETS).get("GRADIO_ADMNUSR")
    if not disclaimer_seen:
        modal_visible = True
        disclaimer_seen = True
    else:
        modal_visible = False
    return [admin_priviledges,
            Modal(visible=modal_visible),
            disclaimer_seen,
            gr.Checkbox(interactive=admin_priviledges),
            gr.Checkbox(interactive=admin_priviledges),
            logging_info]


def toggle_interactivity(is_admin):
    logger.debug("Updating admin functionalities")
    return [gr.UploadButton(file_count="single", interactive=is_admin),
            gr.Tab("Stats", visible=is_admin),
            gr.Checkbox(interactive=is_admin),
            gr.Checkbox(interactive=is_admin)
            ]

def update_stats():
    stats = get_usage_stats()
    return [gr.Plot(plot_cumulative_tokens()), gr.Plot(get_eval_stats_plot()), stats['total_users'], stats['avg_input_tokens_per_user_per_day'], stats['avg_output_tokens_per_user_per_day'], round(stats['avg_input_tokens_per_user_per_day']/stats['avg_output_tokens_per_user_per_day'],2)]

custom_theme = gr.themes.Ocean().set(body_background_fill="linear-gradient(to right top, #f2f2f2, #f1f1f4, #f0f1f5, #eff0f7, #edf0f9, #ebf1fb, #e9f3fd, #e6f4ff, #e4f7ff, #e2faff, #e2fdff, #e3fffd)")

def get_eval_stats_plot():
    # Initialize distribution stats for 'values' fields
    if not os.path.exists("logs/evaluations.jsonl"):
        return []

    values_distributions = defaultdict(list)

    # Process the 'values' field again to get distributions
    with open("logs/evaluations.jsonl", "r", encoding="utf-8") as file:
        for line in file:
            try:
                data = json.loads(line.strip())
                if "evaluation" in data and isinstance(data["evaluation"], dict):
                    for key, value in data["evaluation"].items():
                        values_distributions[key].append(value)
                values_distributions["liked (bool)"].append(int(eval(data["liked"]))*5) #convert bool to int and from 0-1 to 0-5
            except json.JSONDecodeError:
                continue

    numeric_data = {key: values for key, values in values_distributions.items() if all(isinstance(v, (int, float)) or v is None for v in values)}

    # Compute means and standard deviations
    categories = list(numeric_data.keys())
    means = [np.mean([v for v in values if (v is not None and v>=0)]) for values in numeric_data.values()]
    stds = [np.std([v for v in values if (v is not None and v>=0)]) for values in numeric_data.values()]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=categories,
        y=means,
        error_y=dict(type='data', array=stds, visible=True),
        marker_color='#10b981'
    ))
    fig.update_layout(
        title="Evaluation statistics",
        xaxis_title="Categories",
        yaxis_title="Mean and Standard Deviation",
        template="plotly_white"
    )
    return fig

    with open("logs/evaluations.jsonl", "r", encoding="utf-8") as file:
        for i, line in enumerate(file):
            try:
                data = json.loads(line.strip())
                stats["total_records"] += 1

                # Track key occurrences
                for key in data.keys():
                    if key not in stats["keys_frequency"]:
                        stats["keys_frequency"][key] = 0
                    stats["keys_frequency"][key] += 1

                # Collect sample records
                if i < 3:
                    stats["sample_records"].append(data)

            except json.JSONDecodeError:
                continue
def _export(history):
    with open('logs/chat_history.txt', 'w') as f:
        f.write(str(history))
    return 'logs/chat_history.txt'

with gr.Blocks(title="OrientaMed", theme=custom_theme, css="footer {visibility: hidden} #eval_button_submit {color:white} #eval1 {background-color: #dfe7fd} #eval1 fieldset {text-align: center} #eval1 fieldset div {justify-content:center} #eval1 fieldset label {--checkbox-background-color-selected: #7197ff; --checkbox-label-background-fill-selected: #eef3ff; --checkbox-label-background-fill: #dfe7fd; --checkbox-label-border-color-selected: #eef3ff; --checkbox-label-border-color: #dfe7fd; --checkbox-label-background-fill-hover: #eef3ff; --checkbox-background-color-hover:#7197ff} #eval2 {background-color: #e2ece9} #eval2 input {--slider-color: rgb(164 213 199); --input-background-fill:#e2ece9; --neutral-200:#e2ece9} #eval3 {background-color: #fff1e6} #eval3 input {--slider-color: #fbbb65; --input-background-fill:#fff1e6; --neutral-200:#fff1e6} div:has(> #citations_eval) {border:none; box-shadow:none} #citations_eval textarea {font-size: 0.8em} #eval_main_text {text-align: center} #eval_main_text textarea {font-size:1.2em; background-color:#ffffff00}", head="""<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Funnel+Display&display=swap" rel="stylesheet">""") as demo:
    with Modal(visible=False) as modal:
        gr.Markdown("""### ⚠️ Disclaimer / Avviso  

**English:**  
By using this application, you acknowledge that you must not enter any patient-sensitive or personally identifiable health information. Data entered may be shared with third-party services, and any disclosure of patient information is your own responsibility.<br/>This platform is not designed to store or process protected health data.  

Conversations made using this AI tool are not intended to replace the opinion of a medical expert and should always be critically evaluated by a qualified professional. The app is intended to be used as a reference by general practitioners and should not be relied upon as the sole source for medical decision-making.

**Italiano:**  
Utilizzando questa applicazione, riconosci di non dover inserire informazioni sensibili sui pazienti o dati sanitari personali identificabili. I dati inseriti possono essere condivisi con servizi di terze parti e qualsiasi divulgazione di informazioni sui pazienti è sotto la tua responsabilità.<br/>Questa piattaforma non è progettata per archiviare o elaborare dati sanitari protetti.

Le conversazioni effettuate utilizzando questo strumento di intelligenza artificiale non sono destinate a sostituire il parere di un esperto medico e dovrebbero essere sempre valutate criticamente da un professionista qualificato. L'app è destinata ad essere utilizzata come riferimento dai medici di base e non deve essere considerata come l'unica fonte per le decisioni mediche.  
""")
    gr.Markdown(f"<center><h1 style=\042font-size:2.7em; font-family: 'Funnel Display', sans-serif;\042><sup style='color: #61e9b7; font-size:1em;'>+</sup>OrientaMed<span style='color: #61e9b7; font-size:1em;'> .</span></h1></center>")
    admin_state = gr.State(False)
    disclaimer_seen = gr.BrowserState(False)
    kb = gr.Checkbox(label="Usa Knowledge Base", value=True, render=False)
    qa = gr.Checkbox(label="Usa Query Augmentation", value=False, render=False)
    session_state =gr.State()

    with Modal(visible=False) as evalmodal:
        like_dislike_state = gr.State("")
        with gr.Row():
            ec_main_text = gr.Textbox(label="Message", info="Text under evaluation", lines=2, interactive=False, elem_id="eval_main_text")
        with gr.Row():
            with gr.Column(scale=1):
                with gr.Accordion("Medical Accuracy", open=True, elem_id="eval1"):
                    ma1=gr.Radio(choices=[1, 2, 3, 4, 5], interactive=True, value=None, label="Question Comprehension", info="1 = Misunderstood, 5 = Understood")
                    ma2=gr.Radio(choices=[1, 2, 3, 4, 5], interactive=True, value=None, label="Logical Reasoning", info="1 = Illogical, 5 = Logical")
                    ma3=gr.Radio(choices=[1, 2, 3, 4, 5], interactive=True, value=None, label="Alignment with Clinical Guidelines", info="1 = Not Aligned, 5 = Aligned")
                    ma4=gr.Radio(choices=[1, 2, 3, 4, 5], interactive=True, value=None, label="Completeness", info="1 = Incomplete, 5 = Complete")
                with gr.Accordion("Safety", open=False, elem_id="eval2"):
                    sa1=gr.Slider(minimum=1, maximum=5, step=1, value=-1, show_reset_button=False, interactive=True, label="Possibility of Harm", info="1 = Low, 5 = High")
                    sa2=gr.Slider(minimum=1, maximum=5, step=1, value=-1, show_reset_button=False, interactive=True, label="Extent of Possible harm", info="1 = No Harm, 5 = Severe Harm")
                with gr.Accordion("Communication", open=False, elem_id="eval3"):
                    co1=gr.Slider(minimum=1, maximum=5, step=1, value=-1, show_reset_button=False, interactive=True, label="Tone", info="1 = Inappropriate, 5 = Appropriate")
                    co2=gr.Slider(minimum=1, maximum=5, step=1, value=-1, show_reset_button=False, interactive=True, label="Coherence", info="1 = Incoherent, 5 = Coherent")
                    co3=gr.Slider(minimum=1, maximum=5, step=1, value=-1, show_reset_button=False, interactive=True, label="Helpfulness", info="1 = Unhelpful, 5 = Helpful")
            with gr.Column(scale=2):
                with gr.Accordion("Citations",open=False) as ec_cit_accordion:
                    ec_citations = gr.Textbox(label="Citations", placeholder="No citations", lines=10, show_label=False, interactive=True, elem_id="citations_eval")
                cm = gr.Textbox(label="Comments", info="Write your comments here. What could be improved? How?", interactive=True, lines=5)
                tb = gr.Textbox(label="Your answer", info="How would you answer instead? You can copy and paste the original text here to add or edit info or completely rewrite it from scratch.", interactive=True, lines=5)
                submiteval_btn = gr.Button("SUBMIT", variant="primary", elem_id="eval_button_submit")

    eval_components = [("question_comprehension",ma1), ("logical",ma2), ("guidelines_aligment",ma3),
                       ("completeness",ma4),("harm",sa1),("harm_extent", sa2), ("tone",co1),
                       ("coherence",co2),("helpfulness",co3),("main_text",ec_main_text),("citations",ec_citations),("comments",cm),("answer",tb)]

    evalmodal.blur(lambda: [None]*len(eval_components), outputs=[t[1] for t in eval_components])

    with gr.Tab("Chat"):
        history = [{"role": "assistant", "content": random.choice(config.get('gradio').get('greeting-messages'))}]
        chatbot = gr.Chatbot(history, type="messages", show_copy_button=True, layout="panel", resizable=True,
                             avatar_images=(None, config.get('gradio').get("avatar-img")))
        interface = gr.ChatInterface(fn=reply, type="messages",
                                     chatbot=chatbot,
                                     flagging_mode="manual",
                                     flagging_options=config.get('gradio').get('flagging-options'),
                                     flagging_dir="./logs",
                                     save_history=True,
                                     analytics_enabled = False,
                                     examples=[[e] for e in config.get('gradio').get('examples')],
                                     additional_inputs=[admin_state,
                                                        kb,
                                                        qa,
                                                        gr.Textbox(label="Procedure interne, protocolli, anamnesi da affiancare alle linee guida",
                                                                   placeholder="Inserisci qui eventuali procedure interne, protocolli o informazioni aggiuntive riguardanti il paziente. Queste informazioni verranno affiancate alle linee guida nell'elaborazione della risposta.",
                                                                   lines=2,
                                                                   render=False)],
                                     additional_inputs_accordion="Opzioni",
                                     )
        download_btn = gr.Button("Scarica la conversazione", variant='secondary')
        download_btn_hidden = gr.DownloadButton(visible=False, elem_id="download_btn_hidden")
        download_btn.click(fn=_export, inputs=chatbot, outputs=[download_btn_hidden]).then(fn=None, inputs=None,
                                                                                        outputs=None,
                                                                                        js="() => document.querySelector('#download_btn_hidden').click()")


        # Workaround to take into account username and ip address in logs
        # for some reason, overriding the like callback causes two consecutive calls at few ms of distance
        # we set a Session state flag to suppress the second call
        # Session states are not shared between sessions, therefore there should be no concurrency issue
        double_log_flag = gr.State(True)
        def manual_logger(data: gr.LikeData, messages: list, double_log_flag, request: gr.Request):
            if double_log_flag:
                log_filepath = "./logs/log_"+request.username.replace(".","_").replace("/","_") +"_"+ request.client.host.replace(".", "_") + ".csv"
                is_new = not Path(log_filepath).exists()
                csv_data = [json.dumps(messages), data.value, data.index, data.liked, request.client.host, request.username, str(datetime.now())]
                with open(log_filepath, "a", encoding="utf-8", newline="") as csvfile:
                    writer = csv.writer(csvfile)
                    if is_new:
                        writer.writerow(["conversation", "message", "index", "flag", "host",  "username", "timestamp"])
                    writer.writerow(gr.utils.sanitize_list_for_csv(csv_data))
            return not double_log_flag

        def open_modal(data: gr.LikeData):
            if data.liked=="":
                return ["","","",gr.Accordion(open=False),Modal(visible=False)]
            if len(data.value)>1:
                citations = "\n".join(data.value[1:])
            else:
                citations = ""
            return [str(data.liked), gr.Textbox(value=data.value[0]), gr.Textbox(value=citations), gr.Accordion(open=False), Modal(visible=True)]

        interface.chatbot.like(open_modal, outputs=[like_dislike_state, ec_main_text, ec_citations, ec_cit_accordion, evalmodal])

        def usereval(*args):
            global eval_components
            session = args[-1]
            data = {"ip": session["ip"],
                    "username": session["username"],
                    "session_hash": session["session_hash"],
                    "timestamp": str(datetime.now()),
                    "liked": args[-3],
                    "evaluation":dict(zip([c[0] for c in eval_components], args[:-3])),
                    "conversation":json.dumps(args[-2])}
            with open("logs/evaluations.jsonl","a") as file:
                file.write(json.dumps(data) + '\n')
            return [None]*len(args[:-3])+[Modal(visible=False)]

        submiteval_btn.click(usereval, [t[1] for t in eval_components]+[like_dislike_state,chatbot,session_state], [t[1] for t in eval_components]+[evalmodal])

    with gr.Tab("Settings") as settings:
        with gr.Group():
            gr.FileExplorer(label="Knowledge Base",
                            root_dir=config.get('kb-folder'),
                            glob=config.get('globs')[0],
                            interactive=False)
            upload_button = gr.UploadButton(file_count="single", interactive=admin_state.value)
        with gr.Group():
            mfa_input = gr.Textbox(label="AWS MFA token", placeholder="123456")
            btn = gr.Button("Confirm")
    with gr.Tab("Stats", visible=False) as stats_tab:
        with gr.Group():
            stats = get_usage_stats()
            with gr.Row():
                stats_users = gr.Textbox(label="Total users", value=f"{stats['total_users']}", interactive=False)
                stats_input = gr.Textbox(label="Average user input [tokens/dd]", value=f"{stats['avg_input_tokens_per_user_per_day']}", interactive=False)
                stats_output = gr.Textbox(label="Average user output [tokens/dd]", value=f"{stats['avg_output_tokens_per_user_per_day']}", interactive=False)
                stats_ratio = gr.Textbox(label="Input/Output ratio", value=f"{round(stats['avg_input_tokens_per_user_per_day']/stats['avg_output_tokens_per_user_per_day'],2)}", interactive=False)
            with gr.Row():
                stats_plot = gr.Plot(plot_cumulative_tokens())
                eval_plot = gr.Plot(get_eval_stats_plot())
            stats_heat = gr.Plot(plot_daily_tokens_heatmap())
        with gr.Group():
            gr.Image(label="Workflow schema", value=Image.open(io.BytesIO(rag.get_image())))

    gr.HTML("<br><div style='display:flex; justify-content:center; align-items:center'><img src='gradio_api/file=./assets/u.png' style='width:7%; min-width : 100px;'><img src='gradio_api/file=./assets/d.png' style='width:7%; padding-left:1%; padding-right:1%; min-width : 100px;'><img src='gradio_api/file=./assets/b.png' style='width:7%; min-width : 100px;'></div><br><div style='display:flex; justify-content:center; align-items:center'><small>© 2024 - 2025 | BMI Lab 'Mario Stefanelli' | DHEAL-COM | <a href='https://github.com/detsutut/dheal-com-rag-demo'>GitHub</a> </small></div>", elem_id="footer")
    upload_button.upload(upload_file, upload_button, None)
    mfa_input.submit(fn=update_rag, inputs=[mfa_input], outputs=[admin_state,mfa_input])
    btn.click(fn=update_rag, inputs=[mfa_input], outputs=[admin_state,mfa_input])
    admin_state.change(toggle_interactivity, inputs=admin_state, outputs=[upload_button,stats_tab,kb,qa])
    stats_tab.select(update_stats, inputs=None, outputs=[stats_plot, eval_plot, stats_users, stats_input, stats_output, stats_ratio] )
    demo.load(onload, inputs=disclaimer_seen, outputs=[admin_state,modal,disclaimer_seen,kb,qa,session_state])
demo.launch(server_name="0.0.0.0",
            server_port=7860,
            auth=token_auth,
            ssl_keyfile = args.ssl_keyfile,
            ssl_certfile = args.ssl_certfile,
            ssl_verify = False,
            pwa=True,
            favicon_path=config.get('gradio').get('logo-img'),
            allowed_paths=['./assets'])