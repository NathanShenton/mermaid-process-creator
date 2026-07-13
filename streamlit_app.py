import base64
import hmac
import json
import re
import urllib.error
import urllib.request
from textwrap import dedent

import streamlit as st
from openai import OpenAI


st.set_page_config(
    page_title="AI Process Mapper",
    page_icon="🧭",
    layout="wide",
)

SYSTEM_PROMPT = """
You are an expert business process analyst and Mermaid diagram author.

Convert the user's process description into valid Mermaid flowchart code.

Rules:
1. Return only Mermaid code. Do not include markdown fences, headings, notes, or explanations.
2. Use `flowchart TD` unless the user explicitly requests another direction.
3. Use short, clear business-friendly node labels.
4. Use decision diamonds with `{Question?}` and label outgoing routes such as `-- Yes -->` and `-- No -->`.
5. Preserve the process logic. Do not invent unsupported business rules.
6. Use `<br/>` for line breaks inside node labels.
7. Use simple node IDs containing letters and numbers only.
8. Avoid Mermaid syntax that commonly causes rendering failures:
   - no markdown formatting inside labels
   - no unescaped quotation marks
   - avoid parentheses in labels where practical
   - no semicolons
9. When revising a diagram, return the complete revised Mermaid diagram.
10. Ensure every referenced node exists and the final diagram is syntactically coherent.
""".strip()

DEFAULT_PROCESS = """A weekly process starts by querying newly created products from the last six months. If a product has already been audited in the last six months, take no action. Otherwise add it to an audit candidate list. Check whether it has nutritional data. If yes, run an allergen bolding check and an allergen contradiction check. If no, skip those checks. Run an age restriction check for every candidate. Combine the results, write them to a SharePoint list, and notify the relevant business users."""

MERMAID_INK_BASE_URL = "https://mermaid.ink"


def initialise_state() -> None:
    defaults = {
        "authenticated": False,
        "process_description": DEFAULT_PROCESS,
        "mermaid_code": "",
        "editor_text": "",
        "follow_up_input": "",
        "render_error": "",
        "png_bytes": None,
        "pdf_bytes": None,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def clear_all() -> None:
    st.session_state.process_description = DEFAULT_PROCESS
    st.session_state.mermaid_code = ""
    st.session_state.editor_text = ""
    st.session_state.follow_up_input = ""
    st.session_state.render_error = ""
    st.session_state.png_bytes = None
    st.session_state.pdf_bytes = None


def reset_editor() -> None:
    st.session_state.editor_text = st.session_state.mermaid_code


def sign_out() -> None:
    clear_all()
    st.session_state.authenticated = False


initialise_state()


def get_required_secret(name: str) -> str:
    try:
        value = str(st.secrets[name]).strip()
    except (KeyError, FileNotFoundError):
        st.error(
            f"Missing Streamlit secret: `{name}`. "
            "Add it in Manage app → Settings → Secrets."
        )
        st.stop()

    if not value:
        st.error(
            f"Streamlit secret `{name}` is empty. "
            "Add a valid value in Manage app → Settings → Secrets."
        )
        st.stop()

    return value


def show_login() -> None:
    st.title("AI Process Mapper")
    st.caption("Enter the shared access password to continue.")

    with st.form("login_form", clear_on_submit=False):
        entered_password = st.text_input(
            "Access password",
            type="password",
        )
        submitted = st.form_submit_button(
            "Sign in",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        expected_password = get_required_secret("APP_PASSWORD")

        if hmac.compare_digest(
            entered_password.encode("utf-8"),
            expected_password.encode("utf-8"),
        ):
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password.")


if not st.session_state.authenticated:
    show_login()
    st.stop()


OPENAI_API_KEY = get_required_secret("OPENAI_API_KEY")
DEFAULT_MODEL = str(
    st.secrets.get("OPENAI_MODEL", "gpt-4.1-mini")
).strip() or "gpt-4.1-mini"


def clean_mermaid(raw_text: str) -> str:
    text = (raw_text or "").strip()

    fenced = re.search(
        r"```(?:mermaid)?\s*(.*?)```",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        text = fenced.group(1).strip()

    start = re.search(
        r"(?im)^\s*(flowchart|graph)\s+(TD|TB|BT|LR|RL)\b",
        text,
    )
    if start:
        text = text[start.start():].strip()

    return text.replace("\r\n", "\n").strip()


def generate_mermaid(model: str, prompt: str) -> str:
    client = OpenAI(api_key=OPENAI_API_KEY)

    response = client.responses.create(
        model=model,
        instructions=SYSTEM_PROMPT,
        input=prompt,
    )

    result = clean_mermaid(response.output_text)

    if not result:
        raise ValueError("The model returned an empty Mermaid diagram.")

    if not re.match(
        r"(?is)^\s*(flowchart|graph)\s+(TD|TB|BT|LR|RL)\b",
        result,
    ):
        raise ValueError(
            "The model response did not begin with a valid Mermaid "
            "flowchart declaration."
        )

    return result


def mermaid_payload(mermaid_code: str) -> str:
    payload = {
        "code": mermaid_code,
        "mermaid": {
            "theme": "default",
            "flowchart": {
                "htmlLabels": True,
                "curve": "basis",
                "useMaxWidth": True,
            },
        },
        "autoSync": True,
        "updateDiagram": True,
    }

    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def fetch_url_bytes(url: str, timeout: int = 45) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 AI-Process-Mapper/1.0",
            "Accept": "*/*",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read()
            content_type = response.headers.get("Content-Type", "")

            if response.status != 200:
                raise RuntimeError(
                    f"Rendering service returned HTTP {response.status}."
                )

            if not content:
                raise RuntimeError("Rendering service returned an empty response.")

            if "text/html" in content_type.lower():
                preview = content.decode("utf-8", errors="replace")[:500]
                raise RuntimeError(
                    "Rendering service returned an HTML error page: "
                    + preview
                )

            return content

    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(
            f"Rendering service returned HTTP {exc.code}: {error_body}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Could not contact the Mermaid rendering service. "
            f"Details: {exc.reason}"
        ) from exc


def render_diagram_assets(mermaid_code: str) -> tuple[bytes, bytes]:
    encoded = mermaid_payload(mermaid_code)

    png_url = (
        f"{MERMAID_INK_BASE_URL}/img/{encoded}"
        "?type=png&bgColor=white&width=1800"
    )
    pdf_url = f"{MERMAID_INK_BASE_URL}/pdf/{encoded}?fit"

    png = fetch_url_bytes(png_url)
    pdf = fetch_url_bytes(pdf_url)

    if not png.startswith(b"\x89PNG"):
        raise RuntimeError(
            "The rendering service did not return a valid PNG image. "
            "The Mermaid syntax may be invalid."
        )

    if not pdf.startswith(b"%PDF"):
        raise RuntimeError(
            "The rendering service did not return a valid PDF file."
        )

    return png, pdf


def set_diagram(mermaid_code: str) -> None:
    cleaned = clean_mermaid(mermaid_code)

    st.session_state.mermaid_code = cleaned
    st.session_state.editor_text = cleaned
    st.session_state.png_bytes = None
    st.session_state.pdf_bytes = None
    st.session_state.render_error = ""


def refresh_rendered_assets() -> None:
    if not st.session_state.mermaid_code.strip():
        st.session_state.render_error = "There is no Mermaid code to render."
        st.session_state.png_bytes = None
        st.session_state.pdf_bytes = None
        return

    try:
        png, pdf = render_diagram_assets(st.session_state.mermaid_code)
        st.session_state.png_bytes = png
        st.session_state.pdf_bytes = pdf
        st.session_state.render_error = ""
    except Exception as exc:
        st.session_state.png_bytes = None
        st.session_state.pdf_bytes = None
        st.session_state.render_error = str(exc)


st.title("AI Process Mapper")
st.caption(
    "Describe a process, generate Mermaid code, revise it with AI, "
    "edit it directly, and download the visual as a PDF."
)

with st.sidebar:
    st.header("Settings")

    model = st.text_input(
        "OpenAI model",
        value=DEFAULT_MODEL,
        help="Change this only if the configured OpenAI project supports another model.",
    )

    st.success("Access authorised")

    st.divider()

    st.button(
        "Clear diagram and history",
        use_container_width=True,
        on_click=clear_all,
    )

    st.button(
        "Sign out",
        use_container_width=True,
        on_click=sign_out,
    )

    st.caption(
        "The OpenAI API key is stored securely in Streamlit Secrets "
        "and is never shown in the app."
    )

    st.caption(
        "Diagram rendering is provided by the public Mermaid Ink service. "
        "The Mermaid text is sent to that service to create the image and PDF."
    )


left, right = st.columns([0.9, 1.4], gap="large")

with left:
    st.subheader("1. Describe the process")

    st.text_area(
        "Process description",
        key="process_description",
        height=240,
        label_visibility="collapsed",
    )

    if st.button(
        "Generate process diagram",
        type="primary",
        use_container_width=True,
    ):
        if not st.session_state.process_description.strip():
            st.error("Enter a process description first.")
        elif not model.strip():
            st.error("Enter an OpenAI model name.")
        else:
            with st.spinner("Generating Mermaid diagram..."):
                try:
                    prompt = (
                        "Create a Mermaid process flowchart from this "
                        "description:\n\n"
                        + st.session_state.process_description.strip()
                    )

                    generated = generate_mermaid(
                        model=model.strip(),
                        prompt=prompt,
                    )

                    set_diagram(generated)
                    refresh_rendered_assets()
                    st.rerun()

                except Exception as exc:
                    st.error(f"OpenAI request failed: {exc}")

    if st.session_state.mermaid_code:
        st.subheader("2. Ask for a change")

        st.text_area(
            "Describe the change",
            key="follow_up_input",
            placeholder=(
                "For example: Add a manager approval after the results "
                "are combined. If rejected, return the item for rework."
            ),
            height=110,
            label_visibility="collapsed",
        )

        if st.button(
            "Apply AI change",
            use_container_width=True,
        ):
            if not st.session_state.follow_up_input.strip():
                st.error("Describe the change you want to make.")
            else:
                with st.spinner("Updating Mermaid diagram..."):
                    try:
                        revision_prompt = dedent(
                            f"""
                            Here is the current Mermaid diagram:

                            {st.session_state.mermaid_code}

                            Apply this requested change:

                            {st.session_state.follow_up_input.strip()}

                            Return the complete revised Mermaid diagram only.
                            """
                        ).strip()

                        revised = generate_mermaid(
                            model=model.strip(),
                            prompt=revision_prompt,
                        )

                        set_diagram(revised)
                        st.session_state.follow_up_input = ""
                        refresh_rendered_assets()
                        st.rerun()

                    except Exception as exc:
                        st.error(f"OpenAI request failed: {exc}")

        st.subheader("3. Edit Mermaid directly")

        st.text_area(
            "Mermaid code",
            key="editor_text",
            height=320,
            label_visibility="collapsed",
        )

        apply_col, reset_col = st.columns(2)

        with apply_col:
            if st.button(
                "Render edited Mermaid",
                use_container_width=True,
            ):
                cleaned = clean_mermaid(st.session_state.editor_text)

                if not cleaned:
                    st.error("The Mermaid editor is empty.")
                else:
                    st.session_state.mermaid_code = cleaned
                    st.session_state.png_bytes = None
                    st.session_state.pdf_bytes = None
                    st.session_state.render_error = ""

                    with st.spinner("Rendering edited diagram..."):
                        refresh_rendered_assets()

                    st.rerun()

        with reset_col:
            st.button(
                "Reset editor",
                use_container_width=True,
                on_click=reset_editor,
            )

        st.download_button(
            "Download Mermaid code",
            data=st.session_state.mermaid_code,
            file_name="process-diagram.mmd",
            mime="text/plain",
            use_container_width=True,
            on_click="ignore",
        )

with right:
    st.subheader("Diagram")

    if not st.session_state.mermaid_code:
        st.info("Generate a diagram to display it here.")

    else:
        if (
            st.session_state.png_bytes is None
            and not st.session_state.render_error
        ):
            with st.spinner("Rendering diagram..."):
                refresh_rendered_assets()

        if st.session_state.render_error:
            st.error("The diagram could not be rendered.")
            st.code(
                st.session_state.render_error,
                language="text",
            )

            st.info(
                "Review the Mermaid code for syntax problems, edit it on "
                "the left, and select **Render edited Mermaid**."
            )

            if st.button(
                "Try rendering again",
                use_container_width=True,
            ):
                st.session_state.render_error = ""
                refresh_rendered_assets()
                st.rerun()

        elif st.session_state.png_bytes:
            st.image(
                st.session_state.png_bytes,
                caption="Rendered Mermaid process diagram",
                width="stretch",
            )

            download_col1, download_col2 = st.columns(2)

            with download_col1:
                st.download_button(
                    "Download PNG",
                    data=st.session_state.png_bytes,
                    file_name="process-diagram.png",
                    mime="image/png",
                    use_container_width=True,
                    on_click="ignore",
                )

            with download_col2:
                st.download_button(
                    "Download PDF",
                    data=st.session_state.pdf_bytes,
                    file_name="process-diagram.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                    on_click="ignore",
                )

            if st.button(
                "Re-render diagram",
                use_container_width=True,
            ):
                st.session_state.png_bytes = None
                st.session_state.pdf_bytes = None
                st.session_state.render_error = ""
                refresh_rendered_assets()
                st.rerun()


with st.expander("Built-in AI prompt"):
    st.code(SYSTEM_PROMPT, language="text")

st.caption(
    "Python dependencies: streamlit and openai. "
    "The OpenAI key is held in Streamlit Secrets and is not exposed to users."
)
