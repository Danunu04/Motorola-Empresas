"""
BTN_bot - Streamlit para probar el bot de WhatsApp con botones interactivos

Simula la conversación de WhatsApp mostrando botones clickeables
para probar todos los flujos del bot.
"""

import streamlit as st
import httpx
import uuid
import time

API_URL = st.secrets.get("API_URL", "http://localhost:8000")

# Configuración de página
st.set_page_config(
    page_title="Motorola BTN Bot - Test",
    page_icon="📱",
    layout="centered",
)

# Estado de sesión
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
if "messages" not in st.session_state:
    st.session_state.messages = []
if "current_buttons" not in st.session_state:
    st.session_state.current_buttons = []
if "waiting_for_email" not in st.session_state:
    st.session_state.waiting_for_email = False
if "button_counter" not in st.session_state:
    st.session_state.button_counter = 0


def send_message(text: str) -> dict:
    """Envía un mensaje al bot y devuelve la respuesta."""
    try:
        r = httpx.post(
            f"{API_URL}/chat",
            json={
                "session_id": st.session_state.session_id,
                "message": text,
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        return {
            "answer": f"Error de conexión: {e}",
            "buttons": None,
            "took_ms": 0,
        }


def reset_session():
    """Resetea la sesión del bot."""
    try:
        httpx.post(
            f"{API_URL}/reset",
            json={"session_id": st.session_state.session_id},
            timeout=10,
        )
    except httpx.HTTPError:
        pass
    st.session_state.messages = []
    st.session_state.current_buttons = []
    st.session_state.waiting_for_email = False


def add_message(role: str, content: str, buttons: list = None):
    """Agrega un mensaje al historial."""
    st.session_state.messages.append({
        "role": role,
        "content": content,
        "buttons": buttons or [],
    })


def handle_button_click(button_id: str, button_title: str):
    """Maneja el clic en un botón."""
    # Marcar los botones actuales como ya mostrados (para no renderizarlos de nuevo)
    st.session_state.current_buttons = []
    st.session_state.button_counter += 1

    # Mostrar el botón clickeado como mensaje del usuario
    add_message("user", f"🔘 {button_title}")

    # Enviar como button_id al bot
    text = f"button_{button_id}"
    response = send_message(text)

    answer = response.get("answer", "")
    buttons = response.get("buttons")

    add_message("bot", answer, buttons)
    st.session_state.current_buttons = buttons or []
    st.session_state.waiting_for_email = False

    # Si la respuesta pide un email, activar el input de texto
    if "mail" in answer.lower() or "email" in answer.lower() or "correo" in answer.lower():
        st.session_state.waiting_for_email = True


def handle_text_submit():
    """Maneja el envío de texto del usuario."""
    text = st.session_state.text_input_value
    if not text or not text.strip():
        return

    add_message("user", text.strip())
    response = send_message(text.strip())

    answer = response.get("answer", "")
    buttons = response.get("buttons")

    add_message("bot", answer, buttons)
    st.session_state.current_buttons = buttons or []
    st.session_state.waiting_for_email = False

    # Si la respuesta pide un email, activar el input de texto
    if "mail" in answer.lower() or "email" in answer.lower() or "correo" in answer.lower():
        st.session_state.waiting_for_email = True

    st.session_state.text_input_value = ""


# ==============================
# UI
# ==============================

st.title("📱 Motorola BTN Bot - Test")
st.caption(f"Sesión: {st.session_state.session_id[:8]}...")

# Botón de reset
col1, col2 = st.columns([3, 1])
with col2:
    if st.button("🔄 Reset", use_container_width=True):
        reset_session()
        st.rerun()

# Iniciar conversación automáticamente
if not st.session_state.messages:
    response = send_message("hola")
    answer = response.get("answer", "")
    buttons = response.get("buttons")
    add_message("bot", answer, buttons)
    st.session_state.current_buttons = buttons or []

# Mostrar historial de mensajes
for idx, msg in enumerate(st.session_state.messages):
    is_last_bot = (
        msg["role"] != "user"
        and idx == len(st.session_state.messages) - 1
        and st.session_state.current_buttons
    )
    if msg["role"] == "user":
        with st.chat_message("user"):
            st.write(msg["content"])
    else:
        with st.chat_message("assistant"):
            st.write(msg["content"])
            # Mostrar botones debajo del último mensaje del bot
            if is_last_bot:
                buttons = st.session_state.current_buttons
                button_list = []
                for b in buttons:
                    reply = b.get("reply", {})
                    button_id = reply.get("id", "")
                    title = reply.get("title", "")
                    button_list.append((button_id, title))

                # Mostrar botones en columnas de a 3
                for i in range(0, len(button_list), 3):
                    cols = st.columns(min(3, len(button_list) - i))
                    for j, col in enumerate(cols):
                        if i + j < len(button_list):
                            btn_id, btn_title = button_list[i + j]
                            with col:
                                if st.button(
                                    btn_title,
                                    key=f"btn_{btn_id}_{st.session_state.button_counter}_{i}_{j}",
                                    use_container_width=True,
                                ):
                                    handle_button_click(btn_id, btn_title)
                                    st.rerun()

# Input de texto (siempre visible, útil para emails)
st.markdown("---")
text_col, send_col = st.columns([4, 1])

with text_col:
    st.text_input(
        "Escribí un mensaje" if not st.session_state.waiting_for_email else "Ingresá tu email",
        key="text_input_value",
        on_change=handle_text_submit,
        placeholder="Escribí tu email o mensaje...",
    )

with send_col:
    if st.button("Enviar", use_container_width=True):
        handle_text_submit()
        st.rerun()

# Info de la sesión
with st.expander("Info de sesión"):
    st.json({
        "session_id": st.session_state.session_id,
        "messages_count": len(st.session_state.messages),
        "current_buttons": len(st.session_state.current_buttons),
        "waiting_for_email": st.session_state.waiting_for_email,
    })

    if st.button("Ver estados del flujo"):
        try:
            r = httpx.get(f"{API_URL}/flow/states", timeout=10)
            st.json(r.json())
        except httpx.HTTPError as e:
            st.error(f"Error: {e}")

    if st.button("Health check"):
        try:
            r = httpx.get(f"{API_URL}/health", timeout=10)
            st.json(r.json())
        except httpx.HTTPError as e:
            st.error(f"Error: {e}")