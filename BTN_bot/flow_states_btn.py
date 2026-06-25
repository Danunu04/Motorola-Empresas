"""
BTN_bot - Flow States con botones interactivos de WhatsApp

Todos los flujos usan botones interactivos (WhatsApp Interactive Buttons).
El usuario navega exclusivamente haciendo clic en botones, excepto
cuando debe ingresar un email (texto libre).
"""

import re
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import message_store


EMAIL_RE = re.compile(r"[\w\.\-\+]+@[\w\.\-]+\.\w+", re.I)
GENERIC_MAIL_RE = re.compile(r"@(gmail|hotmail|outlook|live|yahoo)\.", re.I)


# ==============================
# Helpers determinísticos
# ==============================

def _normalize(text: str) -> str:
    return (text or "").strip().lower()


def make_easy_support_reply(reply: str) -> str:
    text = str(reply or "").strip()
    if not text:
        return text

    replacements = [
        ("Necesito que me pases", "Pasame"),
        ("No reconocí ese dominio de mail.", "No pude reconocer ese mail."),
        ("No pude determinar el flujo correcto.", "No estoy pudiendo identificar bien el problema."),
        ("Te pido mil disculpas, no estoy entendiendo tu problema.", "Perdón, no estoy terminando de entender el problema."),
        ("Te pido mil disculpas, no estoy pudiendo solucionar tu problema.", "Perdón, no estoy pudiendo resolver esto desde acá."),
        ("Te voy a derivar con una persona más calificada para que pueda revisarlo en detalle.", "Te voy a derivar con una persona más calificada para que lo resuelva con vos."),
        ("Te voy a derivar con una persona más capacitada que te va a ayudar.", "Te voy a derivar con una persona más calificada que te va a ayudar."),
        ("Avísame cuando lo termines.", "Perfecto, avisame cuando termines."),
        ("Cuando lo termines avisame con un sí, listo o hecho.", "Cuando termines, avisame y seguimos."),
        ("Perfecto. Probá nuevamente y contame si funcionó.", "Perfecto. Probá de nuevo y contame si ahora te funcionó."),
        ("Hay un formulario en la página de registro que tenés que completar.", "Hay un formulario de registro que tenés que completar para avanzar."),
        ("Bueno, hay que esperar 48hs para que se termine el registro.", "Perfecto. En ese caso hay que esperar 48 horas para que termine el registro."),
    ]

    for old, new in replacements:
        text = text.replace(old, new)

    return text


def detect_sentiment_basic(user_text: str) -> str:
    text = _normalize(user_text)
    if not text:
        return "neutral"

    if parse_yes_no(text) is True:
        return "positive"
    if parse_yes_no(text) is False:
        return "negative"
    return "neutral"


def parse_yes_no(user_text: str) -> Optional[bool]:
    text = f" {_normalize(user_text)} "

    positive_patterns = [
        r"\bsi\b", r"\bsí\b", r"\bs\b", r"\bsep\b", r"\bdale\b",
        r"\bok\b", r"\boka\b", r"\bde una\b", r"\blisto\b", r"\bya\b",
        r"\bhecho\b", r"\bfunciona\b", r"\bfuncionó\b", r"\bfunciono\b", r"\bpude\b",
        r"\btermin[eé]\b", r"\bperfecto\b", r"\bcorrecto\b"
    ]
    negative_patterns = [
        r"\bno\b", r"\bnop\b", r"\bnah\b", r"\bnegativo\b",
        r"\bno pude\b", r"\bno funciona\b", r"\bno funcionó\b",
        r"\bno me funciona\b", r"\bno me funcionó\b", r"\bno me funciono\b",
        r"\bno me funca\b", r"\bno funca\b",
        r"\btodav[ií]a no\b", r"\berror\b", r"\bfalla\b",
        r"\bno me deja\b", r"\bno entra\b", r"\bno ingresa\b",
        r"\bincorrecto\b", r"\binv[aá]lido\b"
    ]

    for pattern in negative_patterns:
        if re.search(pattern, text):
            return False

    for pattern in positive_patterns:
        if re.search(pattern, text):
            return True

    has_positive = any(re.search(pattern, text) for pattern in positive_patterns)
    has_negative = any(re.search(pattern, text) for pattern in negative_patterns)

    if has_positive and not has_negative:
        return True
    if has_negative and not has_positive:
        return False
    return None


def looks_like_positive_closure(user_text: str) -> bool:
    text = _normalize(user_text)
    if not text:
        return False

    positive_closure_markers = [
        "gracias", "muchas gracias", "perfecto", "joya", "barbaro",
        "bárbaro", "buenisimo", "buenísimo", "excelente", "genial",
        "si funciono", "sí funcionó", "si funcionó", "me funciono",
        "me funcionó", "ya funciona", "quedo resuelto", "quedó resuelto",
    ]
    return any(marker in text for marker in positive_closure_markers)


def looks_like_negative_outcome(user_text: str) -> bool:
    text = _normalize(user_text)
    if not text:
        return False

    negative_outcome_markers = [
        "no funciona", "no funciono", "no funcionó", "no me funciona",
        "no me funciono", "no me funcionó", "no funca", "no me funca",
        "no anduvo", "sigue igual", "todavia no", "todavía no", "no pude",
    ]
    return any(marker in text for marker in negative_outcome_markers)


def looks_like_form_confusion(user_text: str) -> bool:
    text = _normalize(user_text)
    if not text:
        return False

    confusion_markers = [
        "que formulario", "qué formulario", "cual formulario",
        "cuál formulario", "no se que formulario", "no sé qué formulario",
        "no entiendo", "no entendi", "no entendí",
    ]
    return any(marker in text for marker in confusion_markers)


def parse_relative_hours(user_text: str) -> Optional[int]:
    text = _normalize(user_text)

    if "ayer" in text:
        return 24
    if "hoy" in text:
        return 0
    if "reci" in text or "recién" in text or "recien" in text:
        return 1
    if "la semana pasada" in text:
        return 24 * 7

    hour_match = re.search(r"(\d+)\s*(hora|horas|hs|h)\b", text)
    if hour_match:
        return int(hour_match.group(1))

    day_match = re.search(r"(\d+)\s*(dia|días|dias)\b", text)
    if day_match:
        return int(day_match.group(1)) * 24

    if "menos de 48" in text:
        return 47
    if "mas de 48" in text or "más de 48" in text:
        return 49

    return None


def is_generic_email(email: str) -> bool:
    return bool(email and GENERIC_MAIL_RE.search(email))


def classify_email_domain(email: str, company_domains: Dict[str, str]) -> tuple[str, Optional[str]]:
    try:
        domain = email.split("@", 1)[1].lower().strip().lstrip("@")
    except Exception:
        return "unknown", None

    if domain.endswith("gmail.com"):
        return "gmail", None

    code = company_domains.get(domain)
    if code is not None:
        return "company", code

    for base_domain, base_code in company_domains.items():
        if domain.endswith("." + base_domain):
            return "company", base_code

    return "unknown", None


# ==============================
# Datos de sesión
# ==============================

@dataclass
class SessionData:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_name: Optional[str] = None
    email: Optional[str] = None
    code: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def update(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        self.updated_at = datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_name": self.user_name,
            "email": self.email,
            "code": self.code,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": self.metadata.copy(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionData":
        return cls(
            session_id=data.get("session_id", str(uuid.uuid4())),
            user_name=data.get("user_name"),
            email=data.get("email"),
            code=data.get("code"),
            created_at=datetime.fromisoformat(data.get("created_at", datetime.now().isoformat())),
            updated_at=datetime.fromisoformat(data.get("updated_at", datetime.now().isoformat())),
            metadata=data.get("metadata", {}).copy(),
        )


# ==============================
# Base state
# ==============================

@dataclass
class Button:
    """Representa un botón de respuesta rápida de WhatsApp (type: button).

    Reglas WhatsApp:
    - Máximo 3 botones por mensaje
    - title: ≤ 20 caracteres
    """
    id: str
    title: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "reply",
            "reply": {
                "id": self.id,
                "title": self.title[:20],
            }
        }


@dataclass
class ListRow:
    """Representa una fila en un List Message de WhatsApp (type: list).

    Reglas WhatsApp:
    - Máximo 10 filas por sección
    - title: ≤ 24 caracteres
    - description: ≤ 72 caracteres
    """
    id: str
    title: str
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {"id": self.id, "title": self.title[:24]}
        if self.description:
            row["description"] = self.description[:72]
        return row


class FlowState(ABC):
    def __init__(self, state_name: str):
        self.state_name = state_name
        self._lock = threading.Lock()

    @abstractmethod
    def handle(self, context: "FlowContext", user_text: str, llm=None) -> Dict[str, Any]:
        raise NotImplementedError

    def is_terminal(self) -> bool:
        return False

    def get_buttons(self, context: "FlowContext") -> List[Button]:
        return []

    def get_interactive_type(self, context: "FlowContext") -> str:
        """Retorna 'button' o 'list' según el tipo de mensaje interactivo."""
        return "button"

    def get_list_config(self, context: "FlowContext") -> Optional[Dict[str, Any]]:
        """Retorna la configuración del List Message o None.

        Formato esperado:
        {
            "button_text": "Ver opciones",   # ≤ 20 chars
            "sections": [
                {
                    "title": "Sección",       # ≤ 24 chars
                    "rows": [ListRow, ...]
                }
            ]
        }
        """
        return None

    def response(self, context: "FlowContext", reply: str, sentiment: str = "neutral", **extra: Any) -> Dict[str, Any]:
        reply = make_easy_support_reply(reply)
        buttons = self.get_buttons(context)
        interactive_type = self.get_interactive_type(context)
        list_config = self.get_list_config(context)
        payload = {
            "mode": "flow",
            "reply": reply,
            "next": context.to_dict(),
            "sentiment": sentiment,
            "interactive_type": interactive_type,
            "buttons": [btn.to_dict() for btn in buttons] if buttons else None,
        }
        if list_config:
            # Serializar ListRow objects a dicts para transporte JSON
            serialized = {
                "button_text": list_config.get("button_text", "Opciones"),
                "sections": [],
            }
            for section in list_config.get("sections", []):
                sec = {"title": section.get("title", "")}
                rows = []
                for row in section.get("rows", []):
                    if isinstance(row, ListRow):
                        rows.append(row.to_dict())
                    elif isinstance(row, dict):
                        rows.append(row)
                sec["rows"] = rows
                serialized["sections"].append(sec)
            payload["list_config"] = serialized
        payload.update(extra)
        return payload


# ==============================
# Contexto
# ==============================

class FlowContext:
    def __init__(self, flow_spec: Dict[str, Any], company_domains: Dict[str, str]):
        self.flow_spec = flow_spec
        self.company_domains = company_domains
        self._current_state: Optional[FlowState] = None
        self._vars: Dict[str, Any] = {
            "intentos_identificacion": 0,
        }
        self._session_data: SessionData = SessionData()
        self._lock = threading.Lock()

    def set_state(self, state: FlowState) -> None:
        with self._lock:
            self._current_state = state

    def get_state(self) -> Optional[FlowState]:
        with self._lock:
            return self._current_state

    def set_var(self, key: str, value: Any) -> None:
        with self._lock:
            self._vars[key] = value

    def get_var(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._vars.get(key, default)

    def update_vars(self, new_vars: Dict[str, Any]) -> None:
        with self._lock:
            self._vars.update(new_vars)

    def reset(self) -> None:
        with self._lock:
            self._current_state = None
            self._vars = {"intentos_identificacion": 0}
            self._session_data = SessionData()

    def get_session_data(self) -> SessionData:
        with self._lock:
            return SessionData(
                session_id=self._session_data.session_id,
                user_name=self._session_data.user_name,
                email=self._session_data.email,
                code=self._session_data.code,
                created_at=self._session_data.created_at,
                updated_at=self._session_data.updated_at,
                metadata=self._session_data.metadata.copy(),
            )

    def update_session_data(self, **kwargs) -> None:
        with self._lock:
            self._session_data.update(**kwargs)

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self._current_state.state_name if self._current_state else None,
                "vars": self._vars.copy(),
                "session": self._session_data.to_dict(),
            }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], flow_spec: Dict[str, Any], company_domains: Dict[str, str]) -> "FlowContext":
        context = cls(flow_spec, company_domains)
        with context._lock:
            context._vars = data.get("vars", {"intentos_identificacion": 0}).copy()
            session_data = data.get("session", {})
            if session_data:
                context._session_data = SessionData.from_dict(session_data)
            state_name = data.get("state")
            if state_name:
                state = StateFactory.create_state(state_name)
                if state:
                    context._current_state = state
        return context


# ==============================
# Factory
# ==============================

class StateFactory:
    _states: Dict[str, type] = {}
    _lock = threading.Lock()

    @classmethod
    def register(cls, state_name: str):
        def decorator(state_class: type):
            with cls._lock:
                cls._states[state_name] = state_class
            return state_class
        return decorator

    @classmethod
    def create_state(cls, state_name: str) -> Optional[FlowState]:
        with cls._lock:
            state_class = cls._states.get(state_name)
            return state_class(state_name) if state_class else None

    @classmethod
    def get_registered_states(cls) -> List[str]:
        with cls._lock:
            return list(cls._states.keys())


def _set_state_and_reply(context: FlowContext, state_name: str, reply: Optional[str] = None, sentiment: str = "neutral", **extra: Any) -> Dict[str, Any]:
    state = StateFactory.create_state(state_name)
    if state is None:
        raise ValueError(f"Estado no registrado: {state_name}")
    context.set_state(state)
    if reply is None and hasattr(state, "prompt"):
        reply = state.prompt(context)
    return state.response(context, reply or "", sentiment=sentiment, **extra)


# ==============================
# Mensaje de bienvenida fijo
# ==============================

WELCOME_MESSAGE = (
    "Hola, gracias por comunicarte con la Plataforma de Beneficios de Motorola.\n"
    "¿En qué te puedo ayudar hoy?"
)


# ==============================
# Estados concretos - BTN Bot
# ==============================

@StateFactory.register("EstadoInicial")
class EstadoInicial(FlowState):
    """Menú principal con List Message (4 opciones, >3 botones)."""

    def prompt(self, context: FlowContext) -> str:
        return message_store.get_message("welcome_message", default=WELCOME_MESSAGE)

    def get_interactive_type(self, context: FlowContext) -> str:
        return "list"

    def get_list_config(self, context: FlowContext) -> Optional[Dict[str, Any]]:
        return {
            "button_text": message_store.get_message("main_menu_button_text", default="Ver opciones"),
            "sections": [
                {
                    "title": "¿En qué te puedo ayudar?",
                    "rows": [
                        ListRow(
                            "registro",
                            message_store.get_message("main_menu_row_registro_title", default="No me puedo registrar"),
                            message_store.get_message("main_menu_row_registro_description", default="Problemas con el registro"),
                        ),
                        ListRow(
                            "no_veo_precios",
                            message_store.get_message("main_menu_row_no_veo_precios_title", default="No veo precios"),
                            message_store.get_message("main_menu_row_no_veo_precios_description", default="No se muestran los precios"),
                        ),
                        ListRow(
                            "no_veo_descuentos",
                            message_store.get_message("main_menu_row_no_veo_descuentos_title", default="No veo descuentos"),
                            message_store.get_message("main_menu_row_no_veo_descuentos_description", default="Descuentos no aplicados"),
                        ),
                        ListRow(
                            "info_pedido",
                            message_store.get_message("main_menu_row_info_pedido_title", default="Info de mi pedido"),
                            message_store.get_message("main_menu_row_info_pedido_description", default="Consultar estado del pedido"),
                        ),
                    ]
                }
            ]
        }

    def get_buttons(self, context: FlowContext) -> List[Button]:
        return []

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)

        # Manejar respuesta de botón
        if user_text.startswith("button_"):
            button_id = user_text.replace("button_", "")
            context.set_var("intentos_identificacion", 0)
            context.set_var("opcion_inicial", button_id)

            if button_id == "registro":
                context.set_var("flujo", "registro")
                return _set_state_and_reply(context, "EstadoPreFlujo", sentiment=sentiment)

            elif button_id == "no_veo_precios":
                context.set_var("flujo", "precios")
                return _set_state_and_reply(context, "EstadoPreFlujo", sentiment=sentiment)

            elif button_id == "no_veo_descuentos":
                context.set_var("flujo", "descuentos")
                return _set_state_and_reply(context, "EstadoPreFlujo", sentiment=sentiment)

            elif button_id == "info_pedido":
                return _set_state_and_reply(context, "EstadoPreFlujo", sentiment=sentiment)

        # Fallback para texto libre
        attempts = int(context.get_var("intentos_identificacion", 0)) + 1
        context.set_var("intentos_identificacion", attempts)

        if attempts >= 3:
            return _set_state_and_reply(
                context,
                "EstadoSoporte",
                "Perdón, no estoy pudiendo entender tu consulta. Te voy a derivar con una persona más calificada que te va a ayudar. 🤝",
                sentiment=sentiment,
                handoff=True,
            )

        return self.response(
            context,
            message_store.get_message("welcome_message", default=WELCOME_MESSAGE),
            sentiment=sentiment,
        )


# ==============================
# Estado Pre-Flujo: mensaje intermedio antes de cada flujo
# ==============================

PRE_FLUJO_MESSAGE = (
    "Entiendo que acceder a la Plataforma puede ser complicado, "
    "pero te aseguramos que vas a tener buenos beneficios. "
    "Mientras esperas que te atendamos, podés ir probando estos pasos."
)


@StateFactory.register("EstadoPreFlujo")
class EstadoPreFlujo(FlowState):
    """Pantalla intermedia que muestra el mensaje de empatía antes de derivar al flujo."""

    def prompt(self, context: FlowContext) -> str:
        return message_store.get_message("pre_flujo_message", default=PRE_FLUJO_MESSAGE)

    def get_buttons(self, context: FlowContext) -> List[Button]:
        return [Button("continuar", "CONTINUAR")]

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)
        opcion = context.get_var("opcion_inicial", "")

        if user_text.startswith("button_"):
            button_id = user_text.replace("button_", "")
            if button_id == "continuar":
                if opcion == "registro":
                    context.set_var("flujo", "registro")
                    return _set_state_and_reply(
                        context,
                        "EstadoPedirMail",
                        "Sigamos estos pasos así te puedo ayudar a ingresar en la Plataforma de Beneficios.\n\n¿Con qué mail estás intentando ingresar? 📧",
                        sentiment=sentiment,
                    )
                elif opcion == "no_veo_precios":
                    context.set_var("flujo", "precios")
                    return _set_state_and_reply(
                        context,
                        "EstadoPedirMail",
                        "Si ya ingresaste y no ves precios, sigamos estos pasos así podás acceder a los precios con descuento por ser parte de la Plataforma.\n\n¿Con qué mail estás intentando ingresar? 📧",
                        sentiment=sentiment,
                    )
                elif opcion == "no_veo_descuentos":
                    context.set_var("flujo", "descuentos")
                    context.set_var("form_substep", "pregunta_cargaste")
                    return _set_state_and_reply(
                        context,
                        "EstadoNoVeoDescuentos",
                        "Si ingresaste con tu mail personal y no ves los descuentos aplicados, sigamos estos pasos así te ayudo a solucionarlo.\n\nPara comenzar, ¿cargaste el formulario que encontrás en la publicación del beneficio?",
                        sentiment=sentiment,
                    )
                elif opcion == "info_pedido":
                    return _set_state_and_reply(
                        context,
                        "EstadoInfoPedido",
                        "Si necesitás información sobre tu pedido, escribinos por WhatsApp y te ayudamos 😊💙\n\nhttps://wa.me/5491153835784",
                        sentiment=sentiment,
                    )

        # Fallback: si escribe texto libre, derivar según la opción guardada
        if opcion == "registro":
            context.set_var("flujo", "registro")
            return _set_state_and_reply(
                context,
                "EstadoPedirMail",
                "Sigamos estos pasos así te puedo ayudar a ingresar en la Plataforma de Beneficios.\n\n¿Con qué mail estás intentando ingresar? 📧",
                sentiment=sentiment,
            )
        elif opcion == "no_veo_precios":
            context.set_var("flujo", "precios")
            return _set_state_and_reply(
                context,
                "EstadoPedirMail",
                "Si ya ingresaste y no ves precios, sigamos estos pasos así podás acceder a los precios con descuento por ser parte de la Plataforma.\n\n¿Con qué mail estás intentando ingresar? 📧",
                sentiment=sentiment,
            )
        elif opcion == "no_veo_descuentos":
            context.set_var("flujo", "descuentos")
            context.set_var("form_substep", "pregunta_cargaste")
            return _set_state_and_reply(
                context,
                "EstadoNoVeoDescuentos",
                "Si ingresaste con tu mail personal y no ves los descuentos aplicados, sigamos estos pasos así te ayudo a solucionarlo.\n\nPara comenzar, ¿cargaste el formulario que encontrás en la publicación del beneficio?",
                sentiment=sentiment,
            )
        elif opcion == "info_pedido":
            return _set_state_and_reply(
                context,
                "EstadoInfoPedido",
                "Si necesitás información sobre tu pedido, escribinos por WhatsApp y te ayudamos 😊💙\n\nhttps://wa.me/5491153835784",
                sentiment=sentiment,
            )

        # Si no hay opción guardada, volver al inicio
        return _set_state_and_reply(context, "EstadoInicial", sentiment=sentiment)


@StateFactory.register("EstadoNoVeoDescuentos")
class EstadoNoVeoDescuentos(FlowState):
    """Flujo para cuando el usuario no ve los descuentos."""

    def get_buttons(self, context: FlowContext) -> List[Button]:
        substep = context.get_var("descuentos_substep", "pregunta_cargaste")
        if substep == "pregunta_cargaste":
            return [
                Button("si", "✅ Sí, lo cargué"),
                Button("no", "❌ No lo cargué"),
            ]
        elif substep == "pregunta_cuando":
            return [
                Button("menos_48", "Menos de 48hs"),
                Button("mas_48", "Más de 48hs"),
            ]
        elif substep == "esperando_resultado":
            return [
                Button("funciono", "✅ Funcionó"),
                Button("no_funciono", "❌ No funcionó"),
            ]
        return []

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)
        substep = context.get_var("descuentos_substep", "pregunta_cargaste")

        if substep == "pregunta_cargaste":
            # Manejar respuesta de botón
            if user_text.startswith("button_"):
                button_id = user_text.replace("button_", "")
                if button_id == "si":
                    context.set_var("descuentos_substep", "pregunta_cuando")
                    return self.response(
                        context,
                        "¿Cuándo lo cargaste?",
                        sentiment=sentiment,
                    )
                elif button_id == "no":
                    context.set_var("descuentos_substep", "fin")
                    return self.response(
                        context,
                        "Hay un formulario en la publicación del beneficio que tenés que completar. Una vez hecho eso, esperá 48 horas y volvé a intentar. Si seguís sin ver los descuentos, avísame y te derivo con soporte.",
                        sentiment=sentiment,
                    )

            # Fallback texto libre
            if looks_like_form_confusion(user_text):
                context.set_var("descuentos_substep", "fin")
                return _set_state_and_reply(
                    context,
                    "EstadoSoporte",
                    "Hay un formulario de registro en la publicación del beneficio que necesitás completar para ver los descuentos. Te voy a derivar con una persona más calificada para que pueda ayudarte.",
                    sentiment=sentiment,
                    handoff=True,
                )

            answer = parse_yes_no(user_text)
            if answer is True:
                context.set_var("descuentos_substep", "pregunta_cuando")
                return self.response(context, "¿Cuándo lo cargaste?", sentiment=sentiment)
            if answer is False:
                context.set_var("descuentos_substep", "fin")
                return self.response(
                    context,
                    "Hay un formulario en la publicación del beneficio que tenés que completar. Una vez hecho eso, esperá 48 horas y volvé a intentar.",
                    sentiment=sentiment,
                )

            return self.response(context, "Contame si llegaste a cargar el formulario.", sentiment=sentiment)

        if substep == "pregunta_cuando":
            # Manejar respuesta de botón
            if user_text.startswith("button_"):
                button_id = user_text.replace("button_", "")
                if button_id == "menos_48":
                    context.set_var("descuentos_substep", "fin")
                    return self.response(
                        context,
                        "Perfecto. En ese caso hay que esperar 48 horas para que se procese el registro. Una vez que pase ese tiempo, probá de nuevo. 😊",
                        sentiment=sentiment,
                    )
                elif button_id == "mas_48":
                    context.set_var("descuentos_substep", "esperando_resultado")
                    return _set_state_and_reply(
                        context,
                        "EstadoPasosInicioSesion",
                        "Como ya pasaron más de 48 horas, probá iniciando sesión.\n\nHacé click en 'RECIBIR CÓDIGO DE ACCESO POR E-MAIL'. Ingresá tu dirección de correo electrónico y hacé click en 'ENVIAR'.\n\nVas a recibir un código en tu mail. Volvé a la página e ingresalo.",
                        sentiment=sentiment,
                    )

            # Fallback texto libre
            hours = parse_relative_hours(user_text)
            if hours is None:
                return self.response(
                    context,
                    "Decime aproximadamente cuándo lo cargaste.",
                    sentiment=sentiment,
                )

            if hours < 48:
                context.set_var("descuentos_substep", "fin")
                return self.response(
                    context,
                    "Perfecto. En ese caso hay que esperar 48 horas para que se procese el registro. Una vez que pase ese tiempo, probá de nuevo. 😊",
                    sentiment=sentiment,
                )

            context.set_var("descuentos_substep", "esperando_resultado")
            return _set_state_and_reply(
                context,
                "EstadoPasosInicioSesion",
                "Como ya pasaron más de 48 horas, probá iniciando sesión.",
                sentiment=sentiment,
            )

        return self.response(context, "El flujo de descuentos ya quedó resuelto.", sentiment=sentiment)


@StateFactory.register("EstadoInfoPedido")
class EstadoInfoPedido(FlowState):
    """Flujo para información de pedidos."""

    def get_buttons(self, context: FlowContext) -> List[Button]:
        return [
            Button("si", "✅ Sí"),
            Button("no", "❌ No"),
        ]

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)

        # Manejar respuesta de botón
        if user_text.startswith("button_"):
            button_id = user_text.replace("button_", "")
            if button_id == "si":
                return _set_state_and_reply(
                    context,
                    "EstadoInicial",
                    message_store.get_message("welcome_message", default=WELCOME_MESSAGE),
                    sentiment=sentiment,
                )
            elif button_id == "no":
                return _set_state_and_reply(
                    context,
                    "EstadoFinalizado",
                    "Excelente, nos vemos luego. Estoy muy feliz de haberte podido ayudar 😊💙",
                    sentiment=sentiment,
                )

        # Fallback texto libre
        answer = parse_yes_no(user_text)
        if answer is True:
            return _set_state_and_reply(
                context,
                "EstadoInicial",
                message_store.get_message("welcome_message", default=WELCOME_MESSAGE),
                sentiment=sentiment,
            )
        if answer is False:
            return _set_state_and_reply(
                context,
                "EstadoFinalizado",
                "Excelente, nos vemos luego. Estoy muy feliz de haberte podido ayudar 😊💙",
                sentiment=sentiment,
            )

        return self.response(
            context,
            "Si necesitás algo más, elegí una opción. Si no, podés cerrar la conversación.",
            sentiment=sentiment,
        )


@StateFactory.register("EstadoPedirMail")
class EstadoPedirMail(FlowState):
    """Pide el email del usuario."""

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)
        email_match = EMAIL_RE.search(user_text)

        if not email_match:
            return self.response(
                context,
                "Necesito que me pases el mail con el que estás intentando ingresar. 📧",
                sentiment=sentiment,
            )

        email = email_match.group(0)
        context.update_session_data(email=email)
        context.set_var("email_detectado", email)

        flujo = context.get_var("flujo")

        if flujo == "registro":
            domain_type, company_code = classify_email_domain(email, context.company_domains)
            context.set_var("email_domain_type", domain_type)

            if company_code:
                context.update_session_data(code=company_code)
                context.set_var("company_code", company_code)

            if domain_type == "gmail":
                context.set_var("form_substep", "pregunta_cargaste")
                return _set_state_and_reply(
                    context,
                    "EstadoFormulario",
                    "Cargaste el formulario? 📝",
                    sentiment=sentiment,
                )

            if domain_type == "company":
                return _set_state_and_reply(
                    context,
                    "EstadoRegistroMailEmpresa",
                    "El mail parece ser correcto y es un mail empresarial. Como no veo nada incorrecto en el registro, probá nuevamente y decime si te funcionó.",
                    sentiment=sentiment,
                )

            return self.response(
                context,
                "No pude reconocer ese mail. Verificá que lo hayas escrito bien e intentalo de nuevo.",
                sentiment=sentiment,
            )

        if flujo == "precios":
            return _set_state_and_reply(
                context,
                "EstadoPortalBeneficios",
                "Ya iniciaste sesión con tu mail? 🔐",
                sentiment=sentiment,
            )

        if flujo == "login":
            return _set_state_and_reply(
                context,
                "EstadoLogin",
                "Ya habías ingresado antes al portal de beneficios? 🔐",
                sentiment=sentiment,
            )

        return _set_state_and_reply(
            context,
            "EstadoSoporte",
            "No estoy pudiendo identificar bien el problema. Te voy a derivar con una persona más calificada. 🤝",
            sentiment=sentiment,
            handoff=True,
        )


@StateFactory.register("EstadoRegistroMailEmpresa")
class EstadoRegistroMailEmpresa(FlowState):
    def get_buttons(self, context: FlowContext) -> List[Button]:
        return [
            Button("si", "✅ Sí"),
            Button("no", "❌ No"),
        ]

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)

        if user_text.startswith("button_"):
            button_id = user_text.replace("button_", "")
            if button_id == "si":
                return _set_state_and_reply(
                    context,
                    "EstadoConsultaAdicional",
                    "Perfecto, te ayudo con algo más?",
                    sentiment=sentiment,
                )
            elif button_id == "no":
                context.set_var("clear_nav_step", "confirmar")
                return _set_state_and_reply(
                    context,
                    "EstadoBorrarNavegacion",
                    "En ese caso, borremos los datos de navegación del navegador para volver a intentarlo. Te parece?",
                    sentiment=sentiment,
                )

        answer = parse_yes_no(user_text)

        if answer is True:
            return _set_state_and_reply(
                context,
                "EstadoConsultaAdicional",
                "Perfecto, te ayudo con algo más?",
                sentiment=sentiment,
            )

        if answer is False:
            context.set_var("clear_nav_step", "confirmar")
            return _set_state_and_reply(
                context,
                "EstadoBorrarNavegacion",
                "En ese caso, borremos los datos de navegación del navegador para volver a intentarlo. Te parece?",
                sentiment=sentiment,
            )

        return self.response(
            context,
            "Contame si pudiste avanzar con ese mail.",
            sentiment=sentiment,
        )


@StateFactory.register("EstadoLogin")
class EstadoLogin(FlowState):
    def get_buttons(self, context: FlowContext) -> List[Button]:
        return [
            Button("si", "✅ Sí"),
            Button("no", "❌ No"),
        ]

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)

        if user_text.startswith("button_"):
            button_id = user_text.replace("button_", "")
            if button_id == "si":
                context.set_var("login_step", 3)
                return _set_state_and_reply(
                    context,
                    "EstadoPasosInicioSesion",
                    'Si ya recibiste el mail de confirmación con el acceso a la Plataforma, hacé click en "RECIBIR CÓDIGO DE ACCESO POR E-MAIL".\n'
                    'Ingresá tu dirección de correo electrónico, y hacé click en "ENVIAR".\n\n'
                    "Vas a recibir una clave numérica en tu mail. Volvé a la página e ingresalo.\n\n"
                    "Deberías acceder sin problemas",
                    sentiment=sentiment,
                )
            elif button_id == "no":
                return _set_state_and_reply(
                    context,
                    "EstadoFormulario",
                    "Cargaste el formulario? 📝",
                    sentiment=sentiment,
                )

        answer = parse_yes_no(user_text)

        if answer is True:
            context.set_var("login_step", 3)
            return _set_state_and_reply(
                context,
                "EstadoPasosInicioSesion",
                'Hacé click en "RECIBIR CÓDIGO DE ACCESO POR E-MAIL", ingresá tu mail y tocá "ENVIAR". Te llega un código al mail, volvé y cargalo.',
                sentiment=sentiment,
            )

        if answer is False:
            return _set_state_and_reply(
                context,
                "EstadoFormulario",
                "Cargaste el formulario? 📝",
                sentiment=sentiment,
            )

        return self.response(
            context,
            "Contame si ya habías ingresado antes al portal de beneficios.",
            sentiment=sentiment,
        )


@StateFactory.register("EstadoPasosInicioSesion")
class EstadoPasosInicioSesion(FlowState):
    def get_buttons(self, context: FlowContext) -> List[Button]:
        step = int(context.get_var("login_step", 0))
        if step >= 3:
            return [
                Button("funciono", "✅ Funcionó"),
                Button("no_funciono", "❌ No funcionó"),
            ]
        return []

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)
        step = int(context.get_var("login_step", 0))

        if step == 0:
            context.set_var("login_step", 1)
            return self.response(
                context,
                "Hacé click en 'RECIBIR CÓDIGO DE ACCESO POR E-MAIL'.",
                sentiment=sentiment,
            )
        if step == 1:
            context.set_var("login_step", 2)
            return self.response(
                context,
                "Ingresá tu dirección de correo electrónico y hacé click en 'ENVIAR'.",
                sentiment=sentiment,
            )
        if step == 2:
            context.set_var("login_step", 3)
            return self.response(
                context,
                "Vas a recibir un código en tu mail corporativo. Volvé a la página e ingresalo. 📧",
                sentiment=sentiment,
            )

        # Manejar respuesta de botón
        if user_text.startswith("button_"):
            button_id = user_text.replace("button_", "")
            if button_id == "funciono":
                context.set_var("login_step", 0)
                return _set_state_and_reply(
                    context,
                    "EstadoConsultaAdicional",
                    "Perfecto, te ayudo con algo más?",
                    sentiment=sentiment,
                )
            elif button_id == "no_funciono":
                return _set_state_and_reply(
                    context,
                    "EstadoSoporte",
                    "Perdón, no estoy pudiendo resolver esto desde acá. Te voy a derivar con una persona más calificada que lo resuelva con vos. 🤝",
                    sentiment=sentiment,
                    handoff=True,
                )

        answer = parse_yes_no(user_text)
        if answer is True or looks_like_positive_closure(user_text):
            context.set_var("login_step", 0)
            return _set_state_and_reply(
                context,
                "EstadoConsultaAdicional",
                "Perfecto, te ayudo con algo más?",
                sentiment=sentiment,
            )

        if answer is False:
            return _set_state_and_reply(
                context,
                "EstadoSoporte",
                "Perdón, no estoy pudiendo resolver esto desde acá. Te voy a derivar con una persona más calificada que lo resuelva con vos. 🤝",
                sentiment=sentiment,
                handoff=True,
            )

        return self.response(context, "Contame si funcionó.", sentiment=sentiment)


@StateFactory.register("EstadoConsultaAdicional")
class EstadoConsultaAdicional(FlowState):
    def get_buttons(self, context: FlowContext) -> List[Button]:
        return [
            Button("si", "✅ Sí"),
            Button("no", "❌ No"),
        ]

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)

        if user_text.startswith("button_"):
            button_id = user_text.replace("button_", "")
            if button_id == "no":
                return _set_state_and_reply(
                    context,
                    "EstadoFinalizado",
                    "Excelente, nos vemos luego. Estoy muy feliz de haberte podido ayudar 😊💙",
                    sentiment=sentiment,
                )
            elif button_id == "si":
                return _set_state_and_reply(
                    context,
                    "EstadoInicial",
                    message_store.get_message("welcome_message", default=WELCOME_MESSAGE),
                    sentiment=sentiment,
                )

        answer = parse_yes_no(user_text)

        if answer is False:
            return _set_state_and_reply(
                context,
                "EstadoFinalizado",
                "Excelente, nos vemos luego. Estoy muy feliz de haberte podido ayudar 😊💙",
                sentiment=sentiment,
            )

        if answer is True:
            return _set_state_and_reply(
                context,
                "EstadoInicial",
                message_store.get_message("welcome_message", default=WELCOME_MESSAGE),
                sentiment=sentiment,
            )

        return self.response(context, "Perfecto, te ayudo con algo más?", sentiment=sentiment)


@StateFactory.register("EstadoFinalizado")
class EstadoFinalizado(FlowState):
    def is_terminal(self) -> bool:
        return True

    def get_buttons(self, context: FlowContext) -> List[Button]:
        return [
            Button("volver", "Volver al inicio"),
        ]

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)

        if user_text.startswith("button_"):
            button_id = user_text.replace("button_", "")
            if button_id == "volver":
                return _set_state_and_reply(
                    context,
                    "EstadoInicial",
                    message_store.get_message("welcome_message", default=WELCOME_MESSAGE),
                    sentiment=sentiment,
                )

        return self.response(
            context,
            "Excelente, nos vemos luego. Estoy muy feliz de haberte podido ayudar 😊💙",
            sentiment=sentiment,
        )


@StateFactory.register("EstadoFormulario")
class EstadoFormulario(FlowState):
    def get_buttons(self, context: FlowContext) -> List[Button]:
        substep = context.get_var("form_substep", "pregunta_cargaste")
        if substep == "pregunta_cargaste":
            return [
                Button("si", "✅ Sí, lo cargué"),
                Button("no", "❌ No lo cargué"),
            ]
        elif substep == "pregunta_cuando":
            return [
                Button("menos_48", "Menos de 48hs"),
                Button("mas_48", "Más de 48hs"),
            ]
        return []

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)
        substep = context.get_var("form_substep", "pregunta_cargaste")

        if substep == "pregunta_cargaste":
            # Manejar respuesta de botón
            if user_text.startswith("button_"):
                button_id = user_text.replace("button_", "")
                if button_id == "si":
                    context.set_var("form_substep", "pregunta_cuando")
                    return self.response(context, "¿Cuándo lo cargaste?", sentiment=sentiment)
                elif button_id == "no":
                    context.set_var("form_substep", "fin")
                    return self.response(
                        context,
                        "Hay un formulario en la página de registro que tenés que completar. Una vez hecho eso, esperá 48 horas y volvé a intentar.",
                        sentiment=sentiment,
                    )

            if looks_like_form_confusion(user_text):
                context.set_var("form_substep", "fin")
                return _set_state_and_reply(
                    context,
                    "EstadoSoporte",
                    "Hay un formulario de registro en la página que necesitás completar para poder avanzar. Te voy a derivar con una persona más calificada para que pueda ayudarte.",
                    sentiment=sentiment,
                    handoff=True,
                )

            answer = parse_yes_no(user_text)

            if answer is True:
                context.set_var("form_substep", "pregunta_cuando")
                return self.response(context, "¿Cuándo lo cargaste?", sentiment=sentiment)

            if answer is False:
                context.set_var("form_substep", "fin")
                return self.response(
                    context,
                    "Hay un formulario en la página de registro que tenés que completar. Una vez hecho eso, esperá 48 horas y volvé a intentar.",
                    sentiment=sentiment,
                )

            return self.response(context, "Contame si llegaste a cargar el formulario.", sentiment=sentiment)

        if substep == "pregunta_cuando":
            # Manejar respuesta de botón
            if user_text.startswith("button_"):
                button_id = user_text.replace("button_", "")
                if button_id == "menos_48":
                    context.set_var("form_substep", "fin")
                    return self.response(
                        context,
                        "Perfecto. En ese caso hay que esperar 48 horas para que termine el registro. 😊",
                        sentiment=sentiment,
                    )
                elif button_id == "mas_48":
                    context.set_var("form_substep", "fin")
                    return _set_state_and_reply(
                        context,
                        "EstadoPasosInicioSesion",
                        "Como ya pasaron más de 48 horas, probá iniciando sesión.\n\nHacé click en 'RECIBIR CÓDIGO DE ACCESO POR E-MAIL'.",
                        sentiment=sentiment,
                    )

            hours = parse_relative_hours(user_text)
            if hours is None:
                return self.response(
                    context,
                    "Decime aproximadamente cuándo lo cargaste.",
                    sentiment=sentiment,
                )

            if hours < 48:
                context.set_var("form_substep", "fin")
                return self.response(
                    context,
                    "Perfecto. En ese caso hay que esperar 48 horas para que termine el registro. 😊",
                    sentiment=sentiment,
                )

            context.set_var("form_substep", "fin")
            return _set_state_and_reply(
                context,
                "EstadoPasosInicioSesion",
                "Hacé click en 'RECIBIR CÓDIGO DE ACCESO POR E-MAIL'.",
                sentiment=sentiment,
            )

        return self.response(context, "El flujo de formulario ya quedó resuelto.", sentiment=sentiment)


@StateFactory.register("EstadoPortalBeneficios")
class EstadoPortalBeneficios(FlowState):
    def get_buttons(self, context: FlowContext) -> List[Button]:
        return [
            Button("si", "✅ Sí"),
            Button("no", "❌ No"),
        ]

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)

        if user_text.startswith("button_"):
            button_id = user_text.replace("button_", "")
            if button_id == "si":
                return _set_state_and_reply(
                    context,
                    "EstadoBorrarNavegacion",
                    "Lo mejor en este caso es borrar los datos de navegación para asegurarnos de que salga bien. Te parece? 🧹",
                    sentiment=sentiment,
                )
            elif button_id == "no":
                return _set_state_and_reply(
                    context,
                    "EstadoLogin",
                    "Ya habías ingresado antes al portal de beneficios? 🔐",
                    sentiment=sentiment,
                )

        answer = parse_yes_no(user_text)

        if answer is True:
            return _set_state_and_reply(
                context,
                "EstadoBorrarNavegacion",
                "Lo mejor en este caso es borrar los datos de navegación para asegurarnos de que salga bien. Te parece? 🧹",
                sentiment=sentiment,
            )

        if answer is False:
            return _set_state_and_reply(
                context,
                "EstadoLogin",
                "Ya habías ingresado antes al portal de beneficios? 🔐",
                sentiment=sentiment,
            )

        return self.response(
            context,
            "Contame si ya iniciaste sesión en el portal de beneficios.",
            sentiment=sentiment,
        )


@StateFactory.register("EstadoBorrarNavegacion")
class EstadoBorrarNavegacion(FlowState):
    def get_buttons(self, context: FlowContext) -> List[Button]:
        step = context.get_var("clear_nav_step", "confirmar")
        if step in ["confirmar", "sabe_como", "explicar_motivo"]:
            return [
                Button("si", "✅ Sí"),
                Button("no", "❌ No"),
            ]
        elif step == "esperando_confirmacion":
            return [
                Button("listo", "✅ Ya lo hice"),
            ]
        elif step == "finalizado":
            return [
                Button("funciono", "✅ Funcionó"),
                Button("no_funciono", "❌ No funcionó"),
            ]
        return []

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)
        step = context.get_var("clear_nav_step", "confirmar")
        flujo = context.get_var("flujo")
        code = context.get_session_data().code or "{CODIGO}"

        if step == "confirmar":
            if user_text.startswith("button_"):
                button_id = user_text.replace("button_", "")
                if button_id == "si":
                    context.set_var("clear_nav_step", "sabe_como")
                    return self.response(context, "Sabés cómo hacerlo?", sentiment=sentiment)
                elif button_id == "no":
                    context.set_var("clear_nav_step", "explicar_motivo")
                    return self.response(
                        context,
                        "Te cuento por qué te lo pido! A veces el navegador guarda credenciales viejas o incorrectas del portal de beneficios, y eso puede ser justo lo que está causando el problema. Borrando esos datos le damos un reinicio limpio y lo más probable es que todo funcione de una. Sabés cómo hacerlo?",
                        sentiment=sentiment,
                    )

            answer = parse_yes_no(user_text)
            if answer is True:
                context.set_var("clear_nav_step", "sabe_como")
                return self.response(context, "Sabés cómo hacerlo?", sentiment=sentiment)
            if answer is False:
                context.set_var("clear_nav_step", "explicar_motivo")
                return self.response(
                    context,
                    "Te cuento por qué te lo pido! A veces el navegador guarda credenciales viejas o incorrectas del portal de beneficios, y eso puede ser justo lo que está causando el problema. Borrando esos datos le damos un reinicio limpio y lo más probable es que todo funcione de una. Sabés cómo hacerlo?",
                    sentiment=sentiment,
                )
            return self.response(context, "Contame si te parece bien que borremos los datos de navegación.", sentiment=sentiment)

        if step == "explicar_motivo":
            if user_text.startswith("button_"):
                button_id = user_text.replace("button_", "")
                if button_id == "si":
                    context.set_var("clear_nav_step", "esperando_confirmacion")
                    return self.response(context, "Perfecto, avisame cuando termines.", sentiment=sentiment)
                elif button_id == "no":
                    context.set_var("clear_nav_step", "explicar_como")
                    return self.response(
                        context,
                        'Abrí Chrome y tocá en los tres puntos (arriba a la derecha).\nSeleccioná "Historial" y luego "Borrar datos de navegación".\nElegí el intervalo de tiempo y marcá los datos a eliminar.\nTocá en "Borrar datos".\n\nAvisame cuando termines.',
                        sentiment=sentiment,
                    )

            answer = parse_yes_no(user_text)
            if answer is True:
                context.set_var("clear_nav_step", "esperando_confirmacion")
                return self.response(context, "Perfecto, avisame cuando termines.", sentiment=sentiment)
            if answer is False:
                context.set_var("clear_nav_step", "explicar_como")
                return self.response(
                    context,
                    'Abrí Chrome y tocá en los tres puntos (arriba a la derecha).\nSeleccioná "Historial" y luego "Borrar datos de navegación".\nElegí el intervalo de tiempo y marcá los datos a eliminar.\nTocá en "Borrar datos".\n\nAvisame cuando termines.',
                    sentiment=sentiment,
                )
            return self.response(context, "Contame si sabés cómo hacerlo.", sentiment=sentiment)

        if step == "sabe_como":
            if user_text.startswith("button_"):
                button_id = user_text.replace("button_", "")
                if button_id == "si":
                    context.set_var("clear_nav_step", "esperando_confirmacion")
                    return self.response(context, "Perfecto, avisame cuando termines.", sentiment=sentiment)
                elif button_id == "no":
                    context.set_var("clear_nav_step", "explicar_como")
                    return self.response(
                        context,
                        'Abrí Chrome y tocá en los tres puntos (arriba a la derecha).\nSeleccioná "Historial" y luego "Borrar datos de navegación".\nElegí el intervalo de tiempo y marcá los datos a eliminar.\nTocá en "Borrar datos".\n\nAvisame cuando termines.',
                        sentiment=sentiment,
                    )

            answer = parse_yes_no(user_text)
            if answer is True:
                context.set_var("clear_nav_step", "esperando_confirmacion")
                return self.response(context, "Perfecto, avisame cuando termines.", sentiment=sentiment)
            if answer is False:
                context.set_var("clear_nav_step", "explicar_como")
                return self.response(
                    context,
                    'Abrí Chrome y tocá en los tres puntos (arriba a la derecha).\nSeleccioná "Historial" y luego "Borrar datos de navegación".\nElegí el intervalo de tiempo y marcá los datos a eliminar.\nTocá en "Borrar datos".\n\nAvisame cuando termines.',
                    sentiment=sentiment,
                )
            return self.response(context, "Contame si sabés cómo hacerlo.", sentiment=sentiment)

        if step == "explicar_como":
            context.set_var("clear_nav_step", "esperando_confirmacion")
            return self.response(context, "Avisame cuando lo termines.", sentiment=sentiment)

        if step == "esperando_confirmacion":
            if user_text.startswith("button_"):
                button_id = user_text.replace("button_", "")
                if button_id == "listo":
                    context.set_var("clear_nav_step", "finalizado")
                    if flujo == "registro":
                        return self.response(
                            context,
                            f"Ingresá de nuevo, hacé click en registro, ingresá tu mail y usá este {code}.",
                            sentiment=sentiment,
                        )
                    return self.response(context, "Perfecto. Probá de nuevo y contame si funcionó.", sentiment=sentiment)

            answer = parse_yes_no(user_text)
            if answer is not True:
                return self.response(context, "Cuando termines, avisame y seguimos.", sentiment=sentiment)

            context.set_var("clear_nav_step", "finalizado")
            if flujo == "registro":
                return self.response(
                    context,
                    f"Ingresá de nuevo, hacé click en registro, ingresá tu mail y usá este {code}.",
                    sentiment=sentiment,
                )
            return self.response(context, "Perfecto. Probá de nuevo y contame si funcionó.", sentiment=sentiment)

        if step == "finalizado":
            if user_text.startswith("button_"):
                button_id = user_text.replace("button_", "")
                if button_id == "funciono":
                    return _set_state_and_reply(
                        context,
                        "EstadoConsultaAdicional",
                        "Perfecto, te ayudo con algo más?",
                        sentiment=sentiment,
                    )
                elif button_id == "no_funciono":
                    return _set_state_and_reply(
                        context,
                        "EstadoSoporte",
                        "Perdón, no estoy pudiendo resolver esto desde acá. Te voy a derivar con una persona más calificada que lo resuelva con vos. 🤝",
                        sentiment=sentiment,
                        handoff=True,
                    )

            answer = parse_yes_no(user_text)
            if answer is True or looks_like_positive_closure(user_text):
                return _set_state_and_reply(
                    context,
                    "EstadoConsultaAdicional",
                    "Perfecto, te ayudo con algo más?",
                    sentiment=sentiment,
                )
            if answer is False or looks_like_negative_outcome(user_text):
                return _set_state_and_reply(
                    context,
                    "EstadoSoporte",
                    "Perdón, no estoy pudiendo resolver esto desde acá. Te voy a derivar con una persona más calificada que lo resuelva con vos. 🤝",
                    sentiment=sentiment,
                    handoff=True,
                )
            return self.response(context, "Perfecto. Probá de nuevo y contame si funcionó.", sentiment=sentiment)

        return self.response(context, "Ya te indiqué los pasos para borrar navegación.", sentiment=sentiment)


@StateFactory.register("EstadoSoporte")
class EstadoSoporte(FlowState):
    def is_terminal(self) -> bool:
        return True

    def get_buttons(self, context: FlowContext) -> List[Button]:
        return [
            Button("volver", "Volver al inicio"),
        ]

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)

        if user_text.startswith("button_"):
            button_id = user_text.replace("button_", "")
            if button_id == "volver":
                return _set_state_and_reply(
                    context,
                    "EstadoInicial",
                    message_store.get_message("welcome_message", default=WELCOME_MESSAGE),
                    sentiment=sentiment,
                )

        return self.response(
            context,
            "Perdón, no estoy pudiendo solucionar tu problema. Te voy a derivar con una persona más calificada que te va a ayudar. 🤝",
            sentiment=sentiment,
            handoff=True,
        )


# ==============================
# Controlador
# ==============================

class FlowController:
    def __init__(self, flow_spec: Dict[str, Any], company_domains: Dict[str, str]):
        self.flow_spec = flow_spec
        self.company_domains = company_domains
        self._sessions: Dict[str, FlowContext] = {}
        self._lock = threading.Lock()

    def create_context(self, session_id: Optional[str] = None) -> FlowContext:
        context = FlowContext(self.flow_spec, self.company_domains)
        if session_id:
            context.update_session_data(session_id=session_id)
        return context

    def get_or_create_session(self, session_id: str) -> FlowContext:
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = self.create_context(session_id)
            return self._sessions[session_id]

    def restore_context(self, state_dict: Dict[str, Any]) -> FlowContext:
        return FlowContext.from_dict(state_dict, self.flow_spec, self.company_domains)

    def process_message(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        if context.get_state() is None:
            initial_state = StateFactory.create_state("EstadoInicial")
            if initial_state:
                context.set_state(initial_state)

        current_state = context.get_state()
        if current_state is None:
            return {
                "mode": "flow",
                "reply": "",
                "next": context.to_dict(),
                "sentiment": "neutral",
                "buttons": None,
            }

        email_match = EMAIL_RE.search(user_text)
        if email_match:
            context.update_session_data(email=email_match.group(0))

        result = current_state.handle(context, user_text, llm)

        new_state = context.get_state()
        if new_state is not None and new_state.is_terminal():
            context.reset()

        return result

    def get_all_sessions(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {session_id: context.to_dict() for session_id, context in self._sessions.items()}

    def clear_session(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                return True
            return False

    def clear_all_sessions(self) -> None:
        with self._lock:
            self._sessions.clear()


__all__ = [
    "FlowState",
    "FlowContext",
    "SessionData",
    "StateFactory",
    "FlowController",
    "Button",
    "ListRow",
    "WELCOME_MESSAGE",
    "EMAIL_RE",
    "EstadoInicial",
    "EstadoPedirMail",
    "EstadoBorrarNavegacion",
    "EstadoFormulario",
    "EstadoPasosInicioSesion",
    "EstadoPortalBeneficios",
    "EstadoSoporte",
    "EstadoLogin",
    "EstadoNoVeoDescuentos",
    "EstadoInfoPedido",
]
