import html
import json
import re
from textwrap import dedent

import streamlit as st
import streamlit.components.v1 as components
from openai import OpenAI


st.set_page_config(
    page_title="AI Process Mapper",
    page_icon="🧭",
    layout="wide",
)

SYSTEM_PROMPT = """
You are an expert business process analyst and Mermaid diagram author.

Your task is to convert a user's process description into valid Mermaid flowchart markdown.

Rules:
1. Return ONLY the Mermaid code. Do not include markdown fences, commentary, headings, or explanations.
2. Use `flowchart TD` unless the user explicitly requests another direction.
3. Use short, clear node labels suitable for a business process diagram.
4. Represent decisions with diamond nodes using `{Question?}` and label outgoing routes, for example `-- Yes -->` and `-- No -->`.
5. Preserve the logical order and meaning of the user's process. Do not invent unsupported business rules.
6. Use line breaks inside nodes with `<br/>` rather than literal new lines.
7. Use simple node IDs containing letters and numbers only.
8. Avoid Mermaid syntax that commonly breaks rendering:
   - Do not use markdown formatting inside labels.
   - Avoid unescaped quotation marks.
   - Avoid parentheses in node text where possible.
   - Avoid semicolons.
9. When revising an existing diagram, return the complete revised Mermaid diagram, not just the changed lines.
10. Ensure every referenced node is defined and the result is syntactically coherent.
""".strip()

DEFAULT_PROCESS = """A weekly process starts by querying newly created products from the last six months. If a product has already been audited in the last six months, take no action. Otherwise add it to an audit candidate list. Check whether it has nutritional data. If yes, run an allergen bolding check and an allergen contradiction check. If no, skip those checks. Run an age restriction check for every candidate. Combine the results, write them to a SharePoint list, and notify the relevant business users."""


def initialise_state() -> None:
    defaults = {
        "mermaid_code": "",
        "editor_code": "",
        "editor_text": "",
        "history": [],
        "process_description": DEFAULT_PROCESS,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def clean_mermaid(raw_text: str) -> str:
    """Remove code fences and surrounding prose that models sometimes add."""
    text = raw_text.strip()
    fenced = re.search(r"```(?:mermaid)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    start = re.search(r"(?im)^\s*(flowchart|graph)\s+(TD|TB|BT|LR|RL)\b", text)
    if start:
        text = text[start.start():].strip()

    return text.replace("\r\n", "\n").strip()


def generate_mermaid(api_key: str, model: str, user_input: str) -> str:
    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=model,
        instructions=SYSTEM_PROMPT,
        input=user_input,
    )
    result = clean_mermaid(response.output_text)
    if not result:
        raise ValueError("The model returned an empty diagram.")
    return result


def render_mermaid(mermaid_code: str, height: int = 760) -> None:
    """Render Mermaid in an isolated Streamlit HTML component with PDF download."""
    code_json = json.dumps(mermaid_code)
    component_html = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
  <style>
    body {{
      margin: 0;
      padding: 12px;
      font-family: Arial, sans-serif;
      background: white;
      color: #222;
    }}
    .toolbar {{
      display: flex;
      gap: 10px;
      align-items: center;
      margin-bottom: 12px;
      flex-wrap: wrap;
    }}
    button {{
      border: 1px solid #bbb;
      background: #fff;
      border-radius: 7px;
      padding: 8px 12px;
      cursor: pointer;
      font-size: 14px;
    }}
    button:hover {{ background: #f3f3f3; }}
    #status {{ font-size: 13px; color: #666; }}
    #diagram-wrap {{
      overflow: auto;
      border: 1px solid #ddd;
      border-radius: 10px;
      padding: 20px;
      min-height: 560px;
      background: white;
    }}
    #diagram svg {{
      max-width: none !important;
      height: auto;
    }}
    .error {{
      white-space: pre-wrap;
      color: #a40000;
      background: #fff1f1;
      border: 1px solid #efb4b4;
      padding: 12px;
      border-radius: 8px;
    }}
  </style>
</head>
<body>
  <div class="toolbar">
    <button onclick="downloadPDF()">Download PDF</button>
    <button onclick="fitDiagram()">Fit to width</button>
    <span id="status"></span>
  </div>
  <div id="diagram-wrap">
    <div id="diagram"></div>
  </div>

<script>
  const diagramCode = {code_json};
  mermaid.initialize({{
    startOnLoad: false,
    securityLevel: 'loose',
    theme: 'default',
    flowchart: {{ htmlLabels: true, curve: 'basis', useMaxWidth: false }}
  }});

  async function drawDiagram() {{
    const target = document.getElementById('diagram');
    try {{
      const result = await mermaid.render('generated-mermaid-diagram', diagramCode);
      target.innerHTML = result.svg;
      if (result.bindFunctions) result.bindFunctions(target);
      fitDiagram();
    }} catch (error) {{
      target.innerHTML = '<div class="error"><strong>Mermaid rendering error</strong>\n\n' +
        String(error).replaceAll('<', '&lt;').replaceAll('>', '&gt;') + '</div>';
    }}
  }}

  function fitDiagram() {{
    const svg = document.querySelector('#diagram svg');
    if (!svg) return;
    svg.style.width = '100%';
    svg.style.maxWidth = '100%';
    svg.style.height = 'auto';
  }}

  async function downloadPDF() {{
    const status = document.getElementById('status');
    const diagram = document.getElementById('diagram');
    const svg = diagram.querySelector('svg');
    if (!svg) {{
      status.textContent = 'No rendered diagram is available.';
      return;
    }}

    status.textContent = 'Creating PDF...';
    try {{
      const canvas = await html2canvas(diagram, {{
        backgroundColor: '#ffffff',
        scale: 2,
        useCORS: true,
        logging: false
      }});

      const {{ jsPDF }} = window.jspdf;
      const imgData = canvas.toDataURL('image/png');
      const margin = 10;
      const landscape = canvas.width >= canvas.height;
      const pdf = new jsPDF({{
        orientation: landscape ? 'landscape' : 'portrait',
        unit: 'mm',
        format: 'a4'
      }});

      const pageWidth = pdf.internal.pageSize.getWidth();
      const pageHeight = pdf.internal.pageSize.getHeight();
      const maxWidth = pageWidth - margin * 2;
      const maxHeight = pageHeight - margin * 2;
      const ratio = Math.min(maxWidth / canvas.width, maxHeight / canvas.height);
      const imgWidth = canvas.width * ratio;
      const imgHeight = canvas.height * ratio;
      const x = (pageWidth - imgWidth) / 2;
      const y = (pageHeight - imgHeight) / 2;

      pdf.addImage(imgData, 'PNG', x, y, imgWidth, imgHeight, undefined, 'FAST');
      pdf.save('process-diagram.pdf');
      status.textContent = 'PDF downloaded.';
    }} catch (error) {{
      status.textContent = 'PDF creation failed: ' + String(error);
    }}
  }}

  drawDiagram();
</script>
</body>
</html>
"""
    components.html(component_html, height=height, scrolling=True)


initialise_state()


def reset_editor_to_rendered() -> None:
    """Reset the editor before Streamlit instantiates the text-area widget."""
    st.session_state.editor_text = st.session_state.mermaid_code
    st.session_state.editor_code = st.session_state.mermaid_code


st.title("AI Process Mapper")
st.caption("Describe a process, generate Mermaid, refine it with AI, edit the code directly, and download the rendered diagram as PDF.")

with st.sidebar:
    st.header("Settings")
    api_key = st.text_input(
        "OpenAI API key",
        type="password",
        placeholder="sk-...",
        help="The key is used only for API calls during this browser session and is not written to disk by this app.",
    )
    model = st.text_input(
        "OpenAI model",
        value="gpt-4.1-mini",
        help="Change this if your OpenAI project uses a different text model.",
    )
    st.divider()
    if st.button("Clear diagram and history", use_container_width=True):
        st.session_state.mermaid_code = ""
        st.session_state.editor_code = ""
        st.session_state.editor_text = ""
        st.session_state.history = []
        st.rerun()

left, right = st.columns([0.9, 1.4], gap="large")

with left:
    st.subheader("1. Describe the process")
    st.session_state.process_description = st.text_area(
        "Process description",
        value=st.session_state.process_description,
        height=250,
        label_visibility="collapsed",
    )

    if st.button("Generate process diagram", type="primary", use_container_width=True):
        if not api_key.strip():
            st.error("Enter your OpenAI API key first.")
        elif not st.session_state.process_description.strip():
            st.error("Enter a process description first.")
        else:
            with st.spinner("Generating Mermaid diagram..."):
                try:
                    prompt = (
                        "Create a Mermaid process diagram from this description:\n\n"
                        + st.session_state.process_description.strip()
                    )
                    generated = generate_mermaid(api_key.strip(), model.strip(), prompt)
                    st.session_state.mermaid_code = generated
                    st.session_state.editor_code = generated
                    st.session_state.editor_text = generated
                    st.session_state.history = [
                        {"role": "user", "content": st.session_state.process_description.strip()},
                        {"role": "assistant", "content": generated},
                    ]
                    st.rerun()
                except Exception as exc:
                    st.error(f"OpenAI request failed: {exc}")

    if st.session_state.mermaid_code:
        st.subheader("2. Ask for a change")
        follow_up = st.text_area(
            "Describe the change",
            placeholder="For example: Add a manager approval after the audit results are combined. If rejected, return the item for rework.",
            height=120,
            key="follow_up_input",
        )

        if st.button("Apply AI change", use_container_width=True):
            if not api_key.strip():
                st.error("Enter your OpenAI API key first.")
            elif not follow_up.strip():
                st.error("Describe the change you want to make.")
            else:
                with st.spinner("Updating diagram..."):
                    try:
                        revision_prompt = dedent(
                            f"""
                            Here is the current Mermaid diagram:

                            {st.session_state.mermaid_code}

                            Apply this requested change:
                            {follow_up.strip()}

                            Return the complete revised Mermaid diagram only.
                            """
                        ).strip()
                        revised = generate_mermaid(api_key.strip(), model.strip(), revision_prompt)
                        st.session_state.mermaid_code = revised
                        st.session_state.editor_code = revised
                        st.session_state.editor_text = revised
                        st.session_state.history.extend(
                            [
                                {"role": "user", "content": follow_up.strip()},
                                {"role": "assistant", "content": revised},
                            ]
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"OpenAI request failed: {exc}")

        st.subheader("3. Edit Mermaid directly")
        edited_code = st.text_area(
            "Mermaid markdown",
            height=330,
            key="editor_text",
            label_visibility="collapsed",
        )
        col_apply, col_reset = st.columns(2)
        with col_apply:
            if st.button("Render edited Mermaid", use_container_width=True):
                cleaned = clean_mermaid(edited_code)
                if not cleaned:
                    st.error("The Mermaid editor is empty.")
                else:
                    # The text area already owns editor_text during this run.
                    # Updating it here would raise StreamlitAPIException.
                    st.session_state.editor_code = cleaned
                    st.session_state.mermaid_code = cleaned
                    st.rerun()
        with col_reset:
            st.button(
                "Reset editor",
                use_container_width=True,
                on_click=reset_editor_to_rendered,
            )

        st.download_button(
            "Download Mermaid markdown",
            data=st.session_state.mermaid_code,
            file_name="process-diagram.mmd",
            mime="text/plain",
            use_container_width=True,
        )

with right:
    st.subheader("Diagram")
    if st.session_state.mermaid_code:
        render_mermaid(st.session_state.mermaid_code)
    else:
        st.info("Generate a diagram to display it here.")

with st.expander("Built-in AI prompt"):
    st.code(SYSTEM_PROMPT, language="text")

st.caption("Community Cloud requirements: streamlit and openai. Mermaid and PDF libraries are loaded in the browser from public CDNs.")

