"""
BTN_bot - Flow States con botones interactivos de WhatsApp

Todos los flujos usan botones interactivos (WhatsApp Interactive Buttons).
El usuario navega exclusivamente haciendo clic en botones, excepto
cuando debe ingresar un email (texto libre).
"""

import json
import logging
import re
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import message_store


logger = logging.getLogger(__name__)

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


class OptionGroupRenderError(ValueError):
    """Indica que un grupo no puede representarse con interactivos de WhatsApp."""


class OptionRoutingError(ValueError):
    """Indica que una opción dinámica tiene una configuración de ruteo inválida."""


@dataclass
class RenderedOptionGroup:
    """Resultado del render automático de un ``option_group``."""

    interactive_type: str
    buttons: List[Button] = field(default_factory=list)
    list_config: Optional[Dict[str, Any]] = None


def render_option_group(option_group: str) -> RenderedOptionGroup:
    """Renderiza 1-3 opciones como botones y 4-10 como una lista.

    El tipo interactivo se deriva siempre de la cantidad de opciones activas; no
    existe una configuración persistida que permita forzar botones o lista.
    """
    options = message_store.get_option_set(option_group)
    option_count = len(options)

    if not 1 <= option_count <= 10:
        raise OptionGroupRenderError(
            f"El grupo '{option_group}' tiene {option_count} opciones activas; "
            "WhatsApp requiere entre 1 y 10."
        )

    if option_count <= 3:
        return RenderedOptionGroup(
            interactive_type="button",
            buttons=[
                Button(option["option_id"], option["button_title"])
                for option in options
            ],
        )

    config = message_store.get_option_group_config(option_group)
    return RenderedOptionGroup(
        interactive_type="list",
        list_config={
            "button_text": config["button_text"],
            "sections": [
                {
                    "title": config["section_title"],
                    "rows": [
                        ListRow(
                            option["option_id"],
                            option["title"],
                            option.get("description") or "",
                        )
                        for option in options
                    ],
                }
            ],
        },
    )


def _parse_option_json(raw_value: Any, field_name: str, option_group: str, option_id: str) -> Dict[str, Any]:
    if not raw_value:
        return {}
    if isinstance(raw_value, dict):
        return dict(raw_value)
    try:
        parsed = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise OptionRoutingError(
            f"{option_group}/{option_id}: {field_name} no contiene JSON válido"
        ) from exc
    if not isinstance(parsed, dict):
        raise OptionRoutingError(
            f"{option_group}/{option_id}: {field_name} debe contener un objeto JSON"
        )
    return parsed


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

    def get_option_group(self, context: "FlowContext") -> Optional[str]:
        """Retorna el grupo dinámico del estado; None conserva el render legado."""
        return None

    def handle_dynamic_option_override(
        self,
        context: "FlowContext",
        option_group: str,
        option: Dict[str, Any],
        sentiment: str,
        extra: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Hook para las pocas respuestas que deliberadamente siguen en código."""
        return None

    def route_dynamic_option(
        self,
        context: "FlowContext",
        user_text: str,
        sentiment: str = "neutral",
    ) -> Optional[Dict[str, Any]]:
        """Resuelve una selección usando el binding activo del grupo del estado."""
        option_group = self.get_option_group(context)
        if not option_group or not user_text.startswith("button_"):
            return None

        option_id = user_text[len("button_"):]
        option = message_store.get_option_by_id(option_group, option_id)
        if option is None:
            # La opción pudo eliminarse después de haber sido enviada al usuario.
            return _set_state_and_reply(
                context,
                "EstadoSoporte",
                reply=message_store.get_message(
                    "handoff_text",
                    default="Perdón, esa opción ya no está disponible. Te voy a derivar con soporte.",
                ),
                sentiment=sentiment,
                handoff=True,
            )

        context.update_vars(
            _parse_option_json(option.get("target_vars"), "target_vars", option_group, option_id)
        )
        extra = _parse_option_json(
            option.get("extra_flags"), "extra_flags", option_group, option_id
        )

        if option.get("stay_in_state"):
            substep_key = option.get("target_substep_key")
            substep_value = option.get("target_substep_value")
            if not substep_key or substep_value is None:
                raise OptionRoutingError(
                    f"{option_group}/{option_id}: faltan target_substep_key/target_substep_value"
                )
            context.set_var(substep_key, substep_value)

            overridden = self.handle_dynamic_option_override(
                context, option_group, option, sentiment, extra
            )
            if overridden is not None:
                return overridden

            reply_key = option.get("reply_key")
            if not reply_key:
                raise OptionRoutingError(
                    f"{option_group}/{option_id}: reply_key es obligatorio para stay_in_state"
                )
            return self.response(
                context,
                message_store.get_message(reply_key),
                sentiment=sentiment,
                **extra,
            )

        target_state = option.get("target_state") or "EstadoSoporte"
        reply_key = option.get("reply_key")
        reply = message_store.get_message(reply_key) if reply_key else None
        if target_state not in StateFactory.get_registered_states():
            logger.error(
                "[options] target_state inválido en %s/%s: %s; se deriva a soporte",
                option_group,
                option_id,
                target_state,
            )
            target_state = "EstadoSoporte"
        if target_state == "EstadoSoporte":
            reply = reply or message_store.get_message(
                "handoff_text",
                default="Perdón, no estoy pudiendo resolver esto desde acá. Te voy a derivar con soporte.",
            )
            extra["handoff"] = True
        return _set_state_and_reply(
            context,
            target_state,
            reply=reply,
            sentiment=sentiment,
            **extra,
        )

    def response(self, context: "FlowContext", reply: str, sentiment: str = "neutral", **extra: Any) -> Dict[str, Any]:
        reply = make_easy_support_reply(reply)
        option_group = self.get_option_group(context)
        if option_group:
            rendered = render_option_group(option_group)
            buttons = rendered.buttons
            interactive_type = rendered.interactive_type
            list_config = rendered.list_config
        else:
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
    """Menú principal con formato automático según sus opciones activas."""

    def prompt(self, context: FlowContext) -> str:
        return message_store.get_message("welcome_message", default=WELCOME_MESSAGE)

    def get_option_group(self, context: FlowContext) -> Optional[str]:
        return "menu_principal"

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)

        routed = self.route_dynamic_option(context, user_text, sentiment)
        if routed is not None:
            return routed

        # Fallback para texto libre
        attempts = int(context.get_var("intentos_identificacion", 0)) + 1
        context.set_var("intentos_identificacion", attempts)

        if attempts >= 3:
            return _set_state_and_reply(
                context,
                "EstadoSoporte",message_store.get_message("support_fallback_text", default="Perdón, no estoy pudiendo entender tu consulta. Te voy a derivar con una persona más calificada que te va a ayudar. 🤝"),
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
        # Excepción acordada: "continuar" es un mini-router que depende de opcion_inicial y
        # puede elegir cuatro destinos. Podría migrarse en el futuro si cada continuación se
        # modela como una opción independiente con un target único.
        return [
            Button(
                "continuar",
                message_store.get_message("preflujo_continuar_button", default="CONTINUAR"),
            ),
        ]

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
                        "EstadoPedirMail",message_store.get_message("registro_intro_text", default="Sigamos estos pasos así te puedo ayudar a ingresar en la Plataforma de Beneficios.\n\n¿Con qué mail estás intentando ingresar? 📧"),
                        sentiment=sentiment,
                    )
                elif opcion == "no_veo_precios":
                    context.set_var("flujo", "precios")
                    return _set_state_and_reply(
                        context,
                        "EstadoPedirMail",message_store.get_message("precios_intro_text", default="Si ya ingresaste y no ves precios, sigamos estos pasos así podás acceder a los precios con descuento por ser parte de la Plataforma.\n\n¿Con qué mail estás intentando ingresar? 📧"),
                        sentiment=sentiment,
                    )
                elif opcion == "no_veo_descuentos":
                    context.set_var("flujo", "descuentos")
                    context.set_var("form_substep", "pregunta_cargaste")
                    return _set_state_and_reply(
                        context,
                        "EstadoNoVeoDescuentos",message_store.get_message("descuentos_intro_text", default="Si ingresaste con tu mail personal y no ves los descuentos aplicados, sigamos estos pasos así te ayudo a solucionarlo.\n\nPara comenzar, ¿cargaste el formulario que encontrás en la publicación del beneficio?"),
                        sentiment=sentiment,
                    )
                elif opcion == "info_pedido":
                    return _set_state_and_reply(
                        context,
                        "EstadoInfoPedido",message_store.get_message("info_pedido_text", default="Si necesitás información sobre tu pedido, escribinos por WhatsApp y te ayudamos 😊💙\n\nhttps://wa.me/5491153835784"),
                        sentiment=sentiment,
                    )

        # Fallback: si escribe texto libre, derivar según la opción guardada
        if opcion == "registro":
            context.set_var("flujo", "registro")
            return _set_state_and_reply(
                context,
                "EstadoPedirMail",message_store.get_message("registro_intro_text", default="Sigamos estos pasos así te puedo ayudar a ingresar en la Plataforma de Beneficios.\n\n¿Con qué mail estás intentando ingresar? 📧"),
                sentiment=sentiment,
            )
        elif opcion == "no_veo_precios":
            context.set_var("flujo", "precios")
            return _set_state_and_reply(
                context,
                "EstadoPedirMail",message_store.get_message("precios_intro_text", default="Si ya ingresaste y no ves precios, sigamos estos pasos así podás acceder a los precios con descuento por ser parte de la Plataforma.\n\n¿Con qué mail estás intentando ingresar? 📧"),
                sentiment=sentiment,
            )
        elif opcion == "no_veo_descuentos":
            context.set_var("flujo", "descuentos")
            context.set_var("form_substep", "pregunta_cargaste")
            return _set_state_and_reply(
                context,
                "EstadoNoVeoDescuentos",message_store.get_message("descuentos_intro_text", default="Si ingresaste con tu mail personal y no ves los descuentos aplicados, sigamos estos pasos así te ayudo a solucionarlo.\n\nPara comenzar, ¿cargaste el formulario que encontrás en la publicación del beneficio?"),
                sentiment=sentiment,
            )
        elif opcion == "info_pedido":
            return _set_state_and_reply(
                context,
                "EstadoInfoPedido",message_store.get_message("info_pedido_text", default="Si necesitás información sobre tu pedido, escribinos por WhatsApp y te ayudamos 😊💙\n\nhttps://wa.me/5491153835784"),
                sentiment=sentiment,
            )

        # Si no hay opción guardada, volver al inicio
        return _set_state_and_reply(context, "EstadoInicial", sentiment=sentiment)


@StateFactory.register("EstadoNoVeoDescuentos")
class EstadoNoVeoDescuentos(FlowState):
    """Flujo para cuando el usuario no ve los descuentos."""

    def get_option_group(self, context: FlowContext) -> Optional[str]:
        substep = context.get_var("descuentos_substep", "pregunta_cargaste")
        return {
            "pregunta_cargaste": "descuentos_pregunta_cargaste",
            "pregunta_cuando": "descuentos_pregunta_cuando",
        }.get(substep)

    def get_buttons(self, context: FlowContext) -> List[Button]:
        substep = context.get_var("descuentos_substep", "pregunta_cargaste")
        if substep == "esperando_resultado":
            # Dead code conocido: se conserva intacto para revisarlo en una iteración futura.
            return [
                Button("funciono", message_store.get_message("generic_funciono_button", default="✅ Funcionó")),
                Button("no_funciono", message_store.get_message("generic_no_funciono_button", default="❌ No funcionó")),
            ]
        return []

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)
        substep = context.get_var("descuentos_substep", "pregunta_cargaste")

        routed = self.route_dynamic_option(context, user_text, sentiment)
        if routed is not None:
            return routed

        if substep == "pregunta_cargaste":
            # Fallback texto libre
            if looks_like_form_confusion(user_text):
                context.set_var("descuentos_substep", "fin")
                return _set_state_and_reply(
                    context,
                    "EstadoSoporte",message_store.get_message("descuentos_form_confusion_handoff_text", default="Hay un formulario de registro en la publicación del beneficio que necesitás completar para ver los descuentos. Te voy a derivar con una persona más calificada para que pueda ayudarte."),
                    sentiment=sentiment,
                    handoff=True,
                )

            answer = parse_yes_no(user_text)
            if answer is True:
                context.set_var("descuentos_substep", "pregunta_cuando")
                return self.response(context, message_store.get_message("ask_when_loaded_text", default="¿Cuándo lo cargaste?"), sentiment=sentiment)
            if answer is False:
                context.set_var("descuentos_substep", "fin")
                return self.response(
                    context,
                    "Hay un formulario en la publicación del beneficio que tenés que completar. Una vez hecho eso, esperá 48 horas y volvé a intentar.",
                    sentiment=sentiment,
                )

            return self.response(context, message_store.get_message("ask_loaded_form_text", default="Contame si llegaste a cargar el formulario."), sentiment=sentiment)

        if substep == "pregunta_cuando":
            # Fallback texto libre
            hours = parse_relative_hours(user_text)
            if hours is None:
                return self.response(
                    context,message_store.get_message("ask_approx_when_text", default="Decime aproximadamente cuándo lo cargaste."),
                    sentiment=sentiment,
                )

            if hours < 48:
                context.set_var("descuentos_substep", "fin")
                return self.response(
                    context,message_store.get_message("descuentos_wait_48_text", default="Perfecto. En ese caso hay que esperar 48 horas para que se procese el registro. Una vez que pase ese tiempo, probá de nuevo. 😊"),
                    sentiment=sentiment,
                )

            context.set_var("descuentos_substep", "esperando_resultado")
            return _set_state_and_reply(
                context,
                "EstadoPasosInicioSesion",
                "Como ya pasaron más de 48 horas, probá iniciando sesión.",
                sentiment=sentiment,
            )

        return self.response(context, message_store.get_message("descuentos_resolved_text", default="El flujo de descuentos ya quedó resuelto."), sentiment=sentiment)


@StateFactory.register("EstadoInfoPedido")
class EstadoInfoPedido(FlowState):
    """Flujo para información de pedidos."""

    def get_option_group(self, context: FlowContext) -> Optional[str]:
        return "info_pedido_opciones"

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)

        routed = self.route_dynamic_option(context, user_text, sentiment)
        if routed is not None:
            return routed

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
                "EstadoFinalizado",message_store.get_message("goodbye_text", default="Excelente, nos vemos luego. Estoy muy feliz de haberte podido ayudar 😊💙"),
                sentiment=sentiment,
            )

        return self.response(
            context,message_store.get_message("info_pedido_anything_else_text", default="Si necesitás algo más, elegí una opción. Si no, podés cerrar la conversación."),
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
                context,message_store.get_message("ask_email_text", default="Necesito que me pases el mail con el que estás intentando ingresar. 📧"),
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
                    "EstadoFormulario",message_store.get_message("formulario_question_text", default="Cargaste el formulario? 📝"),
                    sentiment=sentiment,
                )

            if domain_type == "company":
                return _set_state_and_reply(
                    context,
                    "EstadoRegistroMailEmpresa",message_store.get_message("pedir_mail_empresa_ok_text", default="El mail parece ser correcto y es un mail empresarial. Como no veo nada incorrecto en el registro, probá nuevamente y decime si te funcionó."),
                    sentiment=sentiment,
                )

            return self.response(
                context,message_store.get_message("unknown_email_domain_text", default="No pude reconocer ese mail. Verificá que lo hayas escrito bien e intentalo de nuevo."),
                sentiment=sentiment,
            )

        if flujo == "precios":
            return _set_state_and_reply(
                context,
                "EstadoPortalBeneficios",message_store.get_message("ask_logged_in_text", default="Ya iniciaste sesión con tu mail? 🔐"),
                sentiment=sentiment,
            )

        if flujo == "login":
            return _set_state_and_reply(
                context,
                "EstadoLogin",message_store.get_message("ask_ingresado_antes_text", default="Ya habías ingresado antes al portal de beneficios? 🔐"),
                sentiment=sentiment,
            )

        return _set_state_and_reply(
            context,
            "EstadoSoporte",message_store.get_message("unknown_problem_handoff_text", default="No estoy pudiendo identificar bien el problema. Te voy a derivar con una persona más calificada. 🤝"),
            sentiment=sentiment,
            handoff=True,
        )


@StateFactory.register("EstadoRegistroMailEmpresa")
class EstadoRegistroMailEmpresa(FlowState):
    def get_option_group(self, context: FlowContext) -> Optional[str]:
        return "registro_mail_empresa_opciones"

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)

        routed = self.route_dynamic_option(context, user_text, sentiment)
        if routed is not None:
            return routed

        answer = parse_yes_no(user_text)

        if answer is True:
            return _set_state_and_reply(
                context,
                "EstadoConsultaAdicional",message_store.get_message("follow_up_help_text", default="Perfecto, te ayudo con algo más?"),
                sentiment=sentiment,
            )

        if answer is False:
            context.set_var("clear_nav_step", "confirmar")
            return _set_state_and_reply(
                context,
                "EstadoBorrarNavegacion",message_store.get_message("borrar_nav_confirm_text", default="En ese caso, borremos los datos de navegación del navegador para volver a intentarlo. Te parece?"),
                sentiment=sentiment,
            )

        return self.response(
            context,message_store.get_message("registro_mail_empresa_ask_progress_text", default="Contame si pudiste avanzar con ese mail."),
            sentiment=sentiment,
        )


@StateFactory.register("EstadoLogin")
class EstadoLogin(FlowState):
    def prompt(self, context: FlowContext) -> str:
        return message_store.get_message(
            "ask_ingresado_antes_text",
            default="¿Ya habías ingresado antes al portal de beneficios? 🔐",
        )

    def get_option_group(self, context: FlowContext) -> Optional[str]:
        return "login_opciones"

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)

        routed = self.route_dynamic_option(context, user_text, sentiment)
        if routed is not None:
            return routed

        answer = parse_yes_no(user_text)

        if answer is True:
            context.set_var("login_step", 3)
            return _set_state_and_reply(
                context,
                "EstadoPasosInicioSesion",message_store.get_message("login_steps_short_text", default="Hacé click en \"RECIBIR CÓDIGO DE ACCESO POR E-MAIL\", ingresá tu mail y tocá \"ENVIAR\". Te llega un código al mail, volvé y cargalo."),
                sentiment=sentiment,
            )

        if answer is False:
            return _set_state_and_reply(
                context,
                "EstadoFormulario",message_store.get_message("formulario_question_text", default="Cargaste el formulario? 📝"),
                sentiment=sentiment,
            )

        return self.response(
            context,
            "Contame si ya habías ingresado antes al portal de beneficios.",
            sentiment=sentiment,
        )


@StateFactory.register("EstadoPasosInicioSesion")
class EstadoPasosInicioSesion(FlowState):
    def get_option_group(self, context: FlowContext) -> Optional[str]:
        if int(context.get_var("login_step", 0)) >= 3:
            return "pasos_inicio_sesion_resultado"
        return None

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)
        step = int(context.get_var("login_step", 0))

        routed = self.route_dynamic_option(context, user_text, sentiment)
        if routed is not None:
            return routed

        if step == 0:
            context.set_var("login_step", 1)
            return self.response(
                context,message_store.get_message("pasos_step1_text", default="Hacé click en 'RECIBIR CÓDIGO DE ACCESO POR E-MAIL'."),
                sentiment=sentiment,
            )
        if step == 1:
            context.set_var("login_step", 2)
            return self.response(
                context,message_store.get_message("pasos_step2_text", default="Ingresá tu dirección de correo electrónico y hacé click en 'ENVIAR'."),
                sentiment=sentiment,
            )
        if step == 2:
            context.set_var("login_step", 3)
            return self.response(
                context,message_store.get_message("pasos_step3_text", default="Vas a recibir un código en tu mail corporativo. Volvé a la página e ingresalo. 📧"),
                sentiment=sentiment,
            )

        answer = parse_yes_no(user_text)
        if answer is True or looks_like_positive_closure(user_text):
            context.set_var("login_step", 0)
            return _set_state_and_reply(
                context,
                "EstadoConsultaAdicional",message_store.get_message("follow_up_help_text", default="Perfecto, te ayudo con algo más?"),
                sentiment=sentiment,
            )

        if answer is False:
            return _set_state_and_reply(
                context,
                "EstadoSoporte",message_store.get_message("handoff_text", default="Perdón, no estoy pudiendo resolver esto desde acá. Te voy a derivar con una persona más calificada que lo resuelva con vos. 🤝"),
                sentiment=sentiment,
                handoff=True,
            )

        return self.response(context, message_store.get_message("pasos_ask_worked_text", default="Contame si funcionó."), sentiment=sentiment)


@StateFactory.register("EstadoConsultaAdicional")
class EstadoConsultaAdicional(FlowState):
    def prompt(self, context: FlowContext) -> str:
        return message_store.get_message(
            "follow_up_help_text", default="Perfecto, ¿te ayudo con algo más?"
        )

    def get_option_group(self, context: FlowContext) -> Optional[str]:
        return "consulta_adicional_opciones"

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)

        routed = self.route_dynamic_option(context, user_text, sentiment)
        if routed is not None:
            return routed

        answer = parse_yes_no(user_text)

        if answer is False:
            return _set_state_and_reply(
                context,
                "EstadoFinalizado",message_store.get_message("goodbye_text", default="Excelente, nos vemos luego. Estoy muy feliz de haberte podido ayudar 😊💙"),
                sentiment=sentiment,
            )

        if answer is True:
            return _set_state_and_reply(
                context,
                "EstadoInicial",
                message_store.get_message("welcome_message", default=WELCOME_MESSAGE),
                sentiment=sentiment,
            )

        return self.response(context, message_store.get_message("follow_up_help_text", default="Perfecto, te ayudo con algo más?"), sentiment=sentiment)


@StateFactory.register("EstadoFinalizado")
class EstadoFinalizado(FlowState):
    def is_terminal(self) -> bool:
        return True

    def prompt(self, context: FlowContext) -> str:
        return message_store.get_message(
            "goodbye_text",
            default="Excelente, nos vemos luego. Estoy muy feliz de haberte podido ayudar 😊💙",
        )

    def get_option_group(self, context: FlowContext) -> Optional[str]:
        return "finalizado_opciones"

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)

        routed = self.route_dynamic_option(context, user_text, sentiment)
        if routed is not None:
            return routed

        return self.response(
            context,message_store.get_message("goodbye_text", default="Excelente, nos vemos luego. Estoy muy feliz de haberte podido ayudar 😊💙"),
            sentiment=sentiment,
        )


@StateFactory.register("EstadoFormulario")
class EstadoFormulario(FlowState):
    def prompt(self, context: FlowContext) -> str:
        return message_store.get_message(
            "formulario_question_text", default="¿Cargaste el formulario? 📝"
        )

    def get_option_group(self, context: FlowContext) -> Optional[str]:
        substep = context.get_var("form_substep", "pregunta_cargaste")
        return {
            "pregunta_cargaste": "formulario_pregunta_cargaste",
            "pregunta_cuando": "formulario_pregunta_cuando",
        }.get(substep)

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)
        substep = context.get_var("form_substep", "pregunta_cargaste")

        routed = self.route_dynamic_option(context, user_text, sentiment)
        if routed is not None:
            return routed

        if substep == "pregunta_cargaste":
            if looks_like_form_confusion(user_text):
                context.set_var("form_substep", "fin")
                return _set_state_and_reply(
                    context,
                    "EstadoSoporte",message_store.get_message("formulario_confusion_handoff_text", default="Hay un formulario de registro en la página que necesitás completar para poder avanzar. Te voy a derivar con una persona más calificada para que pueda ayudarte."),
                    sentiment=sentiment,
                    handoff=True,
                )

            answer = parse_yes_no(user_text)

            if answer is True:
                context.set_var("form_substep", "pregunta_cuando")
                return self.response(context, message_store.get_message("ask_when_loaded_text", default="¿Cuándo lo cargaste?"), sentiment=sentiment)

            if answer is False:
                context.set_var("form_substep", "fin")
                return self.response(
                    context,message_store.get_message("not_loaded_form_text", default="Hay un formulario en la página de registro que tenés que completar. Una vez hecho eso, esperá 48 horas y volvé a intentar."),
                    sentiment=sentiment,
                )

            return self.response(context, message_store.get_message("ask_loaded_form_text", default="Contame si llegaste a cargar el formulario."), sentiment=sentiment)

        if substep == "pregunta_cuando":
            hours = parse_relative_hours(user_text)
            if hours is None:
                return self.response(
                    context,message_store.get_message("ask_approx_when_text", default="Decime aproximadamente cuándo lo cargaste."),
                    sentiment=sentiment,
                )

            if hours < 48:
                context.set_var("form_substep", "fin")
                return self.response(
                    context,message_store.get_message("wait_48_form_text", default="Perfecto. En ese caso hay que esperar 48 horas para que termine el registro. 😊"),
                    sentiment=sentiment,
                )

            context.set_var("form_substep", "fin")
            return _set_state_and_reply(
                context,
                "EstadoPasosInicioSesion",message_store.get_message("pasos_step1_text", default="Hacé click en 'RECIBIR CÓDIGO DE ACCESO POR E-MAIL'."),
                sentiment=sentiment,
            )

        return self.response(context, message_store.get_message("formulario_resolved_text", default="El flujo de formulario ya quedó resuelto."), sentiment=sentiment)


@StateFactory.register("EstadoPortalBeneficios")
class EstadoPortalBeneficios(FlowState):
    def get_option_group(self, context: FlowContext) -> Optional[str]:
        return "portal_beneficios_opciones"

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)

        routed = self.route_dynamic_option(context, user_text, sentiment)
        if routed is not None:
            return routed

        answer = parse_yes_no(user_text)

        if answer is True:
            return _set_state_and_reply(
                context,
                "EstadoBorrarNavegacion",message_store.get_message("portal_beneficios_clear_nav_text", default="Lo mejor en este caso es borrar los datos de navegación para asegurarnos de que salga bien. Te parece? 🧹"),
                sentiment=sentiment,
            )

        if answer is False:
            return _set_state_and_reply(
                context,
                "EstadoLogin",message_store.get_message("ask_ingresado_antes_text", default="Ya habías ingresado antes al portal de beneficios? 🔐"),
                sentiment=sentiment,
            )

        return self.response(
            context,message_store.get_message("portal_beneficios_ask_logged_in_fallback_text", default="Contame si ya iniciaste sesión en el portal de beneficios."),
            sentiment=sentiment,
        )


@StateFactory.register("EstadoBorrarNavegacion")
class EstadoBorrarNavegacion(FlowState):
    def get_option_group(self, context: FlowContext) -> Optional[str]:
        step = context.get_var("clear_nav_step", "confirmar")
        return {
            "confirmar": "borrar_nav_confirmar",
            "explicar_motivo": "borrar_nav_explicar_motivo",
            "sabe_como": "borrar_nav_sabe_como",
            "esperando_confirmacion": "borrar_nav_esperando_confirmacion",
            "finalizado": "borrar_nav_finalizado",
        }.get(step)

    def handle_dynamic_option_override(
        self,
        context: FlowContext,
        option_group: str,
        option: Dict[str, Any],
        sentiment: str,
        extra: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if option_group != "borrar_nav_esperando_confirmacion" or option["option_id"] != "listo":
            return None

        # Excepción acordada: esta respuesta interpola el código de empresa y cambia según
        # el flujo. Se conserva en código porque es el único template dinámico de este tipo.
        flujo = context.get_var("flujo")
        code = context.get_session_data().code or "{CODIGO}"
        if flujo == "registro":
            return self.response(
                context,
                f"{message_store.get_message('borrar_nav_registro_code_text', default='Ingresá de nuevo, hacé click en registro, ingresá tu mail y usá este código:')} {code}.",
                sentiment=sentiment,
                **extra,
            )
        return self.response(
            context,
            message_store.get_message(
                "borrar_nav_try_again_text",
                default="Perfecto. Probá de nuevo y contame si funcionó.",
            ),
            sentiment=sentiment,
            **extra,
        )

    def handle(self, context: FlowContext, user_text: str, llm=None) -> Dict[str, Any]:
        sentiment = detect_sentiment_basic(user_text)
        step = context.get_var("clear_nav_step", "confirmar")
        flujo = context.get_var("flujo")
        code = context.get_session_data().code or "{CODIGO}"

        routed = self.route_dynamic_option(context, user_text, sentiment)
        if routed is not None:
            return routed

        if step == "confirmar":
            answer = parse_yes_no(user_text)
            if answer is True:
                context.set_var("clear_nav_step", "sabe_como")
                return self.response(context, message_store.get_message("borrar_nav_ask_know_how_text", default="Sabés cómo hacerlo?"), sentiment=sentiment)
            if answer is False:
                context.set_var("clear_nav_step", "explicar_motivo")
                return self.response(
                    context,message_store.get_message("borrar_nav_explain_why_text", default="Te cuento por qué te lo pido! A veces el navegador guarda credenciales viejas o incorrectas del portal de beneficios, y eso puede ser justo lo que está causando el problema. Borrando esos datos le damos un reinicio limpio y lo más probable es que todo funcione de una. Sabés cómo hacerlo?"),
                    sentiment=sentiment,
                )
            return self.response(context, message_store.get_message("borrar_nav_ask_agree_text", default="Contame si te parece bien que borremos los datos de navegación."), sentiment=sentiment)

        if step == "explicar_motivo":
            answer = parse_yes_no(user_text)
            if answer is True:
                context.set_var("clear_nav_step", "esperando_confirmacion")
                return self.response(context, message_store.get_message("borrar_nav_wait_finish_text", default="Perfecto, avisame cuando termines."), sentiment=sentiment)
            if answer is False:
                context.set_var("clear_nav_step", "explicar_como")
                return self.response(
                    context,message_store.get_message("borrar_nav_how_to_text", default="Abrí Chrome y tocá en los tres puntos (arriba a la derecha).\nSeleccioná \"Historial\" y luego \"Borrar datos de navegación\".\nElegí el intervalo de tiempo y marcá los datos a eliminar.\nTocá en \"Borrar datos\".\n\nAvisame cuando termines."),
                    sentiment=sentiment,
                )
            return self.response(context, message_store.get_message("borrar_nav_ask_know_how_fallback_text", default="Contame si sabés cómo hacerlo."), sentiment=sentiment)

        if step == "sabe_como":
            answer = parse_yes_no(user_text)
            if answer is True:
                context.set_var("clear_nav_step", "esperando_confirmacion")
                return self.response(context, message_store.get_message("borrar_nav_wait_finish_text", default="Perfecto, avisame cuando termines."), sentiment=sentiment)
            if answer is False:
                context.set_var("clear_nav_step", "explicar_como")
                return self.response(
                    context,message_store.get_message("borrar_nav_how_to_text", default="Abrí Chrome y tocá en los tres puntos (arriba a la derecha).\nSeleccioná \"Historial\" y luego \"Borrar datos de navegación\".\nElegí el intervalo de tiempo y marcá los datos a eliminar.\nTocá en \"Borrar datos\".\n\nAvisame cuando termines."),
                    sentiment=sentiment,
                )
            return self.response(context, message_store.get_message("borrar_nav_ask_know_how_fallback_text", default="Contame si sabés cómo hacerlo."), sentiment=sentiment)

        if step == "explicar_como":
            context.set_var("clear_nav_step", "esperando_confirmacion")
            return self.response(context, message_store.get_message("borrar_nav_ask_finished_text", default="Avisame cuando lo termines."), sentiment=sentiment)

        if step == "esperando_confirmacion":
            answer = parse_yes_no(user_text)
            if answer is not True:
                return self.response(context, message_store.get_message("borrar_nav_wait_again_text", default="Cuando termines, avisame y seguimos."), sentiment=sentiment)

            context.set_var("clear_nav_step", "finalizado")
            if flujo == "registro":
                return self.response(
                    context,
                    f"{message_store.get_message('borrar_nav_registro_code_text', default='Ingresá de nuevo, hacé click en registro, ingresá tu mail y usá este código:')} {code}.",
                    sentiment=sentiment,
                )
            return self.response(context, message_store.get_message("borrar_nav_try_again_text", default="Perfecto. Probá de nuevo y contame si funcionó."), sentiment=sentiment)

        if step == "finalizado":
            answer = parse_yes_no(user_text)
            if answer is True or looks_like_positive_closure(user_text):
                return _set_state_and_reply(
                    context,
                    "EstadoConsultaAdicional",message_store.get_message("follow_up_help_text", default="Perfecto, te ayudo con algo más?"),
                    sentiment=sentiment,
                )
            if answer is False or looks_like_negative_outcome(user_text):
                return _set_state_and_reply(
                    context,
                    "EstadoSoporte",message_store.get_message("handoff_text", default="Perdón, no estoy pudiendo resolver esto desde acá. Te voy a derivar con una persona más calificada que lo resuelva con vos. 🤝"),
                    sentiment=sentiment,
                    handoff=True,
                )
            return self.response(context, message_store.get_message("borrar_nav_try_again_text", default="Perfecto. Probá de nuevo y contame si funcionó."), sentiment=sentiment)

        return self.response(context, message_store.get_message("borrar_nav_steps_done_text", default="Ya te indiqué los pasos para borrar navegación."), sentiment=sentiment)


@StateFactory.register("EstadoSoporte")
class EstadoSoporte(FlowState):
    def is_terminal(self) -> bool:
        return True

    def get_buttons(self, context: FlowContext) -> List[Button]:
        return [
            Button("volver", message_store.get_message("volver_inicio_button", default="Volver al inicio")),
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
            context,message_store.get_message("soporte_handoff_text", default="Perdón, no estoy pudiendo solucionar tu problema. Te voy a derivar con una persona más calificada que te va a ayudar. 🤝"),
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
    "RenderedOptionGroup",
    "OptionGroupRenderError",
    "OptionRoutingError",
    "render_option_group",
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
