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
8. Quote every node label so punctuation is treated as text, for example:
   - `A["Process owned by Operator (FBO)"]`
   - `B{"Approved by manager?"}`
9. Never output an unquoted node label such as `A[Process (FBO)]`.
10. Inside labels:
   - use single quotation marks rather than double quotation marks
   - use `and` instead of ampersands where practical
   - do not use markdown formatting
   - do not use semicolons
11. Define every node ID exactly once.
12. Never define a node first as a rectangle and later redefine it as a decision.
13. Decision nodes must use the diamond shape on their first and only definition, for example `A2{"Order found?<br/>Automation Suitable"}`.
14. Do not use the lowercase word `end` as a node ID. Use `EndNode["End"]` instead.
15. When revising a diagram, return the complete revised Mermaid diagram.
16. Ensure every referenced node exists and the final diagram is syntactically coherent.
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
        "diagram_theme": "Standard",
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


def invalidate_rendered_assets() -> None:
    """Force the diagram to be regenerated when its visual style changes."""
    st.session_state.png_bytes = None
    st.session_state.pdf_bytes = None
    st.session_state.render_error = ""


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


def quote_mermaid_node_labels(mermaid_code: str) -> str:
    """
    Quote common Mermaid node labels so punctuation is treated as text.

    Examples:
        A[Handled by Operator (FBO)]
        becomes
        A["Handled by Operator (FBO)"]

        B{Manager approved?}
        becomes
        B{"Manager approved?"}

    Already quoted labels are left unchanged. Double quotation marks inside
    labels are converted to single quotation marks so they cannot terminate
    the Mermaid label early.
    """

    def quote_square(match: re.Match) -> str:
        node_id = match.group("node_id")
        label = match.group("label").strip()

        if len(label) >= 2 and label.startswith('"') and label.endswith('"'):
            return match.group(0)

        safe_label = label.replace('"', "'")
        return f'{node_id}["{safe_label}"]'

    def quote_decision(match: re.Match) -> str:
        node_id = match.group("node_id")
        label = match.group("label").strip()

        if len(label) >= 2 and label.startswith('"') and label.endswith('"'):
            return match.group(0)

        safe_label = label.replace('"', "'")
        return f'{node_id}{{"{safe_label}"}}'

    # Standard rectangular nodes: A[Label]
    square_pattern = re.compile(
        r'(?P<node_id>\b[A-Za-z][A-Za-z0-9_]*)'
        r'\[(?P<label>[^\[\]\n]*)\]'
    )

    # Decision nodes: B{Question?}
    decision_pattern = re.compile(
        r'(?P<node_id>\b[A-Za-z][A-Za-z0-9_]*)'
        r'\{(?P<label>[^{}\n]*)\}'
    )

    lines = []
    for line in mermaid_code.splitlines():
        line = square_pattern.sub(quote_square, line)
        line = decision_pattern.sub(quote_decision, line)
        lines.append(line)

    return "\n".join(lines)


def normalise_mermaid_structure(mermaid_code: str) -> str:
    """
    Remove duplicate standalone node declarations and avoid Mermaid's reserved
    lowercase `end` identifier.

    AI-generated diagrams sometimes define a node twice, for example:

        A2["Check whether order exists"]
        A2{"Order found?"}

    Mermaid treats the second declaration as a syntax error. This function
    keeps the final declaration for each node ID because it usually contains
    the intended decision shape.
    """

    lines = mermaid_code.splitlines()

    node_definition = re.compile(
        r'^\s*(?P<node_id>[A-Za-z][A-Za-z0-9_]*)\s*'
        r'(?P<shape>\[(?P<square>.*)\]|\{(?P<decision>.*)\})\s*$'
    )

    last_definition_index: dict[str, int] = {}

    for index, line in enumerate(lines):
        match = node_definition.match(line)
        if match:
            last_definition_index[match.group("node_id")] = index

    output_lines: list[str] = []

    for index, line in enumerate(lines):
        match = node_definition.match(line)

        if match and last_definition_index.get(match.group("node_id")) != index:
            continue

        line = re.sub(
            r'(?P<arrow>-->|---|-.->|==>)\s*end\b',
            r'\g<arrow> EndNode["End"]',
            line,
        )

        output_lines.append(line)

    return "\n".join(output_lines)


def clean_mermaid(raw_text: str) -> str:
    """Clean model output and make common node labels Mermaid-safe."""
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

    text = text.replace("\r\n", "\n").strip()
    text = normalise_mermaid_structure(text)
    return quote_mermaid_node_labels(text)


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


def apply_diagram_theme(
    mermaid_code: str,
    theme_name: str,
) -> str:
    """
    Apply colours according to each node's role in the process.

    Colourful mode uses:
    - green for starting nodes
    - blue for ordinary process steps
    - yellow for decision diamonds
    - red for ending nodes

    Automation wording inside labels does not affect styling.
    """
    if theme_name != "Colourful by node type":
        return mermaid_code

    node_definition = re.compile(
        r'^\s*(?P<node_id>[A-Za-z][A-Za-z0-9_]*)\s*'
        r'(?P<shape>\[(?P<square>.*)\]|\{(?P<decision>.*)\})\s*$'
    )

    node_ids: set[str] = set()
    decision_nodes: set[str] = set()

    for line in mermaid_code.splitlines():
        match = node_definition.match(line)
        if not match:
            continue

        node_id = match.group("node_id")
        node_ids.add(node_id)

        if match.group("decision") is not None:
            decision_nodes.add(node_id)

    incoming: dict[str, int] = {node_id: 0 for node_id in node_ids}
    outgoing: dict[str, int] = {node_id: 0 for node_id in node_ids}

    # Capture the source and destination IDs for common Mermaid arrows,
    # including arrows with labels such as: A -- Yes --> B
    edge_pattern = re.compile(
        r'(?P<source>\b[A-Za-z][A-Za-z0-9_]*\b)'
        r'(?:\s*--[^>\n]*-->\s*|\s*-->\s*|\s*-.->\s*|\s*==>\s*)'
        r'(?P<target>\b[A-Za-z][A-Za-z0-9_]*\b)'
    )

    for line in mermaid_code.splitlines():
        for match in edge_pattern.finditer(line):
            source = match.group("source")
            target = match.group("target")

            if source in outgoing:
                outgoing[source] += 1
            if target in incoming:
                incoming[target] += 1

    start_nodes = {
        node_id
        for node_id in node_ids
        if incoming.get(node_id, 0) == 0
    }

    end_nodes = {
        node_id
        for node_id in node_ids
        if outgoing.get(node_id, 0) == 0
    }

    # Start/end colours take priority over the ordinary process colour.
    ordinary_nodes = (
        node_ids
        - decision_nodes
        - start_nodes
        - end_nodes
    )

    # A node can technically be both a decision and an end. Keep ending-node
    # styling as the final priority because it communicates completion.
    decision_only_nodes = decision_nodes - start_nodes - end_nodes

    style_lines = [
        "",
        "%% Automatically applied node-type theme",
        "classDef startNode fill:#DCFCE7,stroke:#16A34A,color:#14532D,stroke-width:2px",
        "classDef processNode fill:#DBEAFE,stroke:#2563EB,color:#1E3A8A,stroke-width:2px",
        "classDef decisionNode fill:#FEF3C7,stroke:#D97706,color:#78350F,stroke-width:2px",
        "classDef endNode fill:#FEE2E2,stroke:#DC2626,color:#7F1D1D,stroke-width:2px",
    ]

    if ordinary_nodes:
        style_lines.append(
            f"class {','.join(sorted(ordinary_nodes))} processNode"
        )

    if decision_only_nodes:
        style_lines.append(
            f"class {','.join(sorted(decision_only_nodes))} decisionNode"
        )

    if start_nodes:
        style_lines.append(
            f"class {','.join(sorted(start_nodes))} startNode"
        )

    if end_nodes:
        style_lines.append(
            f"class {','.join(sorted(end_nodes))} endNode"
        )

    return mermaid_code.rstrip() + "\n" + "\n".join(style_lines)

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


def render_diagram_assets(
    mermaid_code: str,
    theme_name: str,
) -> tuple[bytes, bytes]:
    # Apply the safety pass immediately before rendering as well. This also
    # protects diagrams loaded from an older session or manually edited code.
    safe_mermaid_code = normalise_mermaid_structure(mermaid_code)
    safe_mermaid_code = quote_mermaid_node_labels(safe_mermaid_code)
    safe_mermaid_code = apply_diagram_theme(
        safe_mermaid_code,
        theme_name,
    )
    encoded = mermaid_payload(safe_mermaid_code)

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
        png, pdf = render_diagram_assets(
            st.session_state.mermaid_code,
            st.session_state.diagram_theme,
        )
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

    st.selectbox(
        "Diagram style",
        options=[
            "Standard",
            "Colourful by node type",
        ],
        key="diagram_theme",
        help=(
            "Standard uses Mermaid's normal appearance. Colourful by node type "
            "uses green for starting nodes, blue for ordinary process steps, "
            "yellow for decisions, and red for ending nodes."
        ),
        on_change=invalidate_rendered_assets,
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

    if st.session_state.diagram_theme == "Colourful by node type":
        st.caption(
            "Colour key: green = start · blue = process step · "
            "yellow = decision · red = end"
        )

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
