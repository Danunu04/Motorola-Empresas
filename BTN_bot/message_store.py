"""
message_store.py - Cache en memoria de mensajes editables del bot.

Capa intermedia entre bot.py y la tabla BigQuery `mensajes_editables`. Carga todos
los mensajes en memoria con un TTL de 5 minutos para que el bot no haga una
query a BigQuery por cada mensaje en cada conversación.

Interfaz publica:
    get_message(key, default="") -> str
    set_message(key, content, updated_by="") -> None
    load_all_messages() -> Dict[str, str]
    invalidate_cache() -> None
    get_all_messages_with_metadata() -> List[Dict[str, Any]]
    create_message(...) -> None
    update_message_order(orders) -> None
"""

import os
import time
import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Optional

try:
    from google.cloud import bigquery
except ImportError:
    bigquery = None

logger = logging.getLogger(__name__)

# ==============================
# Configuracion
# ==============================

BIGQUERY_DATASET = os.getenv("BIGQUERY_CHAT_LOG_DATASET", "inspectia_logs")
BIGQUERY_BOT_MESSAGES_TABLE = os.getenv("BIGQUERY_BOT_MESSAGES_TABLE", "mensajes_editables")
LEGACY_BOT_MESSAGES_TABLE = "bot_messages"
CACHE_TTL_SECONDS = 5 * 60

MESSAGE_LIMITS: Dict[str, int] = {
    "button": 20,
    "list_row_title": 24,
    "list_row_description": 72,
    "text": 2000,
    "button_text": 2000,
}

# ==============================
# Catalogo de defaults (extraido de flow_states_btn.py)
# ==============================

DEFAULT_MESSAGES: List[Dict[str, Any]] = [
    # -- Global / compartidos --
    {
        "message_key": "welcome_message",
        "message_type": "text",
        "state_name": None,
        "flujo_identificacion_mensaje": "bienvenida",
        "label": "Mensaje de bienvenida",
        "default_content": (
            "Hola, gracias por comunicarte con la Plataforma de Beneficios de Motorola.\n"
            "¿En qué te puedo ayudar hoy?"
        ),
        "orden": 10,
    },
    {
        "message_key": "generic_yes_button",
        "message_type": "button",
        "state_name": None,
        "flujo_identificacion_mensaje": "si",
        "label": "Botón Sí",
        "default_content": "✅ Sí",
        "orden": 20,
    },
    {
        "message_key": "generic_no_button",
        "message_type": "button",
        "state_name": None,
        "flujo_identificacion_mensaje": "no",
        "label": "Botón No",
        "default_content": "❌ No",
        "orden": 30,
    },
    {
        "message_key": "generic_funciono_button",
        "message_type": "button",
        "state_name": None,
        "flujo_identificacion_mensaje": "funciono",
        "label": "Botón Funcionó",
        "default_content": "✅ Funcionó",
        "orden": 40,
    },
    {
        "message_key": "generic_no_funciono_button",
        "message_type": "button",
        "state_name": None,
        "flujo_identificacion_mensaje": "noFunciono",
        "label": "Botón No funcionó",
        "default_content": "❌ No funcionó",
        "orden": 50,
    },
    {
        "message_key": "generic_si_loaded_button",
        "message_type": "button",
        "state_name": None,
        "flujo_identificacion_mensaje": "siCargue",
        "label": "Botón Sí, lo cargué",
        "default_content": "✅ Sí, lo cargué",
        "orden": 60,
    },
    {
        "message_key": "generic_no_loaded_button",
        "message_type": "button",
        "state_name": None,
        "flujo_identificacion_mensaje": "noCargue",
        "label": "Botón No lo cargué",
        "default_content": "❌ No lo cargué",
        "orden": 70,
    },
    {
        "message_key": "generic_menos_48_button",
        "message_type": "button",
        "state_name": None,
        "flujo_identificacion_mensaje": "menos48hs",
        "label": "Botón Menos de 48hs",
        "default_content": "Menos de 48hs",
        "orden": 80,
    },
    {
        "message_key": "generic_mas_48_button",
        "message_type": "button",
        "state_name": None,
        "flujo_identificacion_mensaje": "mas48hs",
        "label": "Botón Más de 48hs",
        "default_content": "Más de 48hs",
        "orden": 90,
    },
    {
        "message_key": "generic_listo_button",
        "message_type": "button",
        "state_name": None,
        "flujo_identificacion_mensaje": "yaLoHice",
        "label": "Botón Ya lo hice",
        "default_content": "✅ Ya lo hice",
        "orden": 100,
    },
    {
        "message_key": "volver_inicio_button",
        "message_type": "button",
        "state_name": None,
        "flujo_identificacion_mensaje": "volverInicio",
        "label": "Botón Volver al inicio",
        "default_content": "Volver al inicio",
        "orden": 110,
    },
    {
        "message_key": "follow_up_help_text",
        "message_type": "text",
        "state_name": None,
        "flujo_identificacion_mensaje": "ayudaAdicional",
        "label": "Pregunta de ayuda adicional",
        "default_content": "Perfecto, te ayudo con algo más?",
        "orden": 120,
    },
    {
        "message_key": "goodbye_text",
        "message_type": "text",
        "state_name": None,
        "flujo_identificacion_mensaje": "despedida",
        "label": "Mensaje de despedida",
        "default_content": "Excelente, nos vemos luego. Estoy muy feliz de haberte podido ayudar 😊💙",
        "orden": 130,
    },
    {
        "message_key": "handoff_text",
        "message_type": "text",
        "state_name": None,
        "flujo_identificacion_mensaje": "derivacionSoporte",
        "label": "Mensaje de derivación a soporte",
        "default_content": "Perdón, no estoy pudiendo resolver esto desde acá. Te voy a derivar con una persona más calificada que lo resuelva con vos. 🤝",
        "orden": 140,
    },
    {
        "message_key": "support_fallback_text",
        "message_type": "text",
        "state_name": None,
        "flujo_identificacion_mensaje": "noEntiendoDerivacion",
        "label": "Mensaje de derivación por no entender",
        "default_content": "Perdón, no estoy pudiendo entender tu consulta. Te voy a derivar con una persona más calificada que te va a ayudar. 🤝",
        "orden": 150,
    },
    {
        "message_key": "ask_email_text",
        "message_type": "text",
        "state_name": "EstadoPedirMail",
        "flujo_identificacion_mensaje": "pedirEmail",
        "label": "Solicitud de email",
        "default_content": "Necesito que me pases el mail con el que estás intentando ingresar. 📧",
        "orden": 160,
    },
    {
        "message_key": "unknown_email_domain_text",
        "message_type": "text",
        "state_name": "EstadoPedirMail",
        "flujo_identificacion_mensaje": "emailNoReconocido",
        "label": "Mail no reconocido",
        "default_content": "No pude reconocer ese mail. Verificá que lo hayas escrito bien e intentalo de nuevo.",
        "orden": 170,
    },
    {
        "message_key": "ask_loaded_form_text",
        "message_type": "text",
        "state_name": "EstadoFormulario",
        "flujo_identificacion_mensaje": "preguntaFormularioCargado",
        "label": "Pregunta si cargó el formulario",
        "default_content": "Contame si llegaste a cargar el formulario.",
        "orden": 180,
    },
    {
        "message_key": "ask_when_loaded_text",
        "message_type": "text",
        "state_name": "EstadoFormulario",
        "flujo_identificacion_mensaje": "preguntaCuandoCargado",
        "label": "Pregunta cuándo cargó el formulario",
        "default_content": "¿Cuándo lo cargaste?",
        "orden": 190,
    },
    {
        "message_key": "ask_approx_when_text",
        "message_type": "text",
        "state_name": "EstadoFormulario",
        "flujo_identificacion_mensaje": "preguntaAproximadamenteCuando",
        "label": "Pedido de aproximación temporal",
        "default_content": "Decime aproximadamente cuándo lo cargaste.",
        "orden": 200,
    },
    {
        "message_key": "ask_logged_in_text",
        "message_type": "text",
        "state_name": None,
        "flujo_identificacion_mensaje": "preguntaInicioSesion",
        "label": "Pregunta si inició sesión",
        "default_content": "Ya iniciaste sesión con tu mail? 🔐",
        "orden": 210,
    },
    {
        "message_key": "ask_ingresado_antes_text",
        "message_type": "text",
        "state_name": None,
        "flujo_identificacion_mensaje": "preguntaIngresoAnterior",
        "label": "Pregunta si ingresó antes",
        "default_content": "Ya habías ingresado antes al portal de beneficios? 🔐",
        "orden": 220,
    },
    {
        "message_key": "not_loaded_form_text",
        "message_type": "text",
        "state_name": "EstadoFormulario",
        "flujo_identificacion_mensaje": "formularioNoCargado",
        "label": "Indicación de formulario no cargado",
        "default_content": "Hay un formulario en la página de registro que tenés que completar. Una vez hecho eso, esperá 48 horas y volvé a intentar.",
        "orden": 230,
    },
    {
        "message_key": "wait_48_form_text",
        "message_type": "text",
        "state_name": "EstadoFormulario",
        "flujo_identificacion_mensaje": "esperar48hs",
        "label": "Indicación de esperar 48hs",
        "default_content": "Perfecto. En ese caso hay que esperar 48 horas para que termine el registro. 😊",
        "orden": 240,
    },
    {
        "message_key": "login_steps_short_text",
        "message_type": "text",
        "state_name": None,
        "flujo_identificacion_mensaje": "pasosLoginCorto",
        "label": "Pasos breves de inicio de sesión",
        "default_content": "Hacé click en \"RECIBIR CÓDIGO DE ACCESO POR E-MAIL\", ingresá tu mail y tocá \"ENVIAR\". Te llega un código al mail, volvé y cargalo.",
        "orden": 250,
    },
    {
        "message_key": "unknown_problem_handoff_text",
        "message_type": "text",
        "state_name": None,
        "flujo_identificacion_mensaje": "problemaNoIdentificado",
        "label": "Mensaje de derivación por problema no identificado",
        "default_content": "No estoy pudiendo identificar bien el problema. Te voy a derivar con una persona más calificada. 🤝",
        "orden": 260,
    },
    {
        "message_key": "info_pedido_text",
        "message_type": "text",
        "state_name": None,
        "flujo_identificacion_mensaje": "infoPedido",
        "label": "Link de WhatsApp para pedidos",
        "default_content": (
            "Si necesitás información sobre tu pedido, escribinos por WhatsApp y te ayudamos 😊💙\n"
            "\n"
            "https://wa.me/5491153835784"
        ),
        "orden": 270,
    },
    {
        "message_key": "registro_intro_text",
        "message_type": "text",
        "state_name": "EstadoFormulario",
        "flujo_identificacion_mensaje": "registroIntro",
        "label": "Introducción al flujo de registro",
        "default_content": (
            "Sigamos estos pasos así te puedo ayudar a ingresar en la Plataforma de Beneficios.\n"
            "\n"
            "¿Con qué mail estás intentando ingresar? 📧"
        ),
        "orden": 280,
    },
    {
        "message_key": "precios_intro_text",
        "message_type": "text",
        "state_name": "EstadoPortalBeneficios",
        "flujo_identificacion_mensaje": "preciosIntro",
        "label": "Introducción al flujo de precios",
        "default_content": (
            "Si ya ingresaste y no ves precios, sigamos estos pasos así podás acceder a los precios con descuento por ser parte de la Plataforma.\n"
            "\n"
            "¿Con qué mail estás intentando ingresar? 📧"
        ),
        "orden": 290,
    },
    {
        "message_key": "descuentos_intro_text",
        "message_type": "text",
        "state_name": "EstadoNoVeoDescuentos",
        "flujo_identificacion_mensaje": "descuentosIntro",
        "label": "Introducción al flujo de descuentos",
        "default_content": (
            "Si ingresaste con tu mail personal y no ves los descuentos aplicados, sigamos estos pasos así te ayudo a solucionarlo.\n"
            "\n"
            "Para comenzar, ¿cargaste el formulario que encontrás en la publicación del beneficio?"
        ),
        "orden": 300,
    },
    {
        "message_key": "formulario_question_text",
        "message_type": "text",
        "state_name": "EstadoFormulario",
        "flujo_identificacion_mensaje": "preguntaFormulario",
        "label": "Texto pregunta formulario",
        "default_content": "Cargaste el formulario? 📝",
        "orden": 310,
    },
    # -- EstadoInicial --
    {
        "message_key": "main_menu_button_text",
        "message_type": "button_text",
        "state_name": "EstadoInicial",
        "flujo_identificacion_mensaje": "menuVerOpciones",
        "label": "Texto del botón del menú principal",
        "default_content": "Ver opciones",
        "orden": 10,
    },
    {
        "message_key": "main_menu_row_registro_title",
        "message_type": "list_row_title",
        "state_name": "EstadoInicial",
        "flujo_identificacion_mensaje": "menuRegistroTitulo",
        "label": "Título - No me puedo registrar",
        "default_content": "No me puedo registrar",
        "orden": 20,
    },
    {
        "message_key": "main_menu_row_registro_description",
        "message_type": "list_row_description",
        "state_name": "EstadoInicial",
        "flujo_identificacion_mensaje": "menuRegistroDescripcion",
        "label": "Descripción - No me puedo registrar",
        "default_content": "Problemas con el registro",
        "orden": 30,
    },
    {
        "message_key": "main_menu_row_no_veo_precios_title",
        "message_type": "list_row_title",
        "state_name": "EstadoInicial",
        "flujo_identificacion_mensaje": "menuPreciosTitulo",
        "label": "Título - No veo precios",
        "default_content": "No veo precios",
        "orden": 40,
    },
    {
        "message_key": "main_menu_row_no_veo_precios_description",
        "message_type": "list_row_description",
        "state_name": "EstadoInicial",
        "flujo_identificacion_mensaje": "menuPreciosDescripcion",
        "label": "Descripción - No veo precios",
        "default_content": "No se muestran los precios",
        "orden": 50,
    },
    {
        "message_key": "main_menu_row_no_veo_descuentos_title",
        "message_type": "list_row_title",
        "state_name": "EstadoInicial",
        "flujo_identificacion_mensaje": "menuDescuentosTitulo",
        "label": "Título - No veo descuentos",
        "default_content": "No veo descuentos",
        "orden": 60,
    },
    {
        "message_key": "main_menu_row_no_veo_descuentos_description",
        "message_type": "list_row_description",
        "state_name": "EstadoInicial",
        "flujo_identificacion_mensaje": "menuDescuentosDescripcion",
        "label": "Descripción - No veo descuentos",
        "default_content": "Descuentos no aplicados",
        "orden": 70,
    },
    {
        "message_key": "main_menu_row_info_pedido_title",
        "message_type": "list_row_title",
        "state_name": "EstadoInicial",
        "flujo_identificacion_mensaje": "menuPedidoTitulo",
        "label": "Título - Info de mi pedido",
        "default_content": "Info de mi pedido",
        "orden": 80,
    },
    {
        "message_key": "main_menu_row_info_pedido_description",
        "message_type": "list_row_description",
        "state_name": "EstadoInicial",
        "flujo_identificacion_mensaje": "menuPedidoDescripcion",
        "label": "Descripción - Info de mi pedido",
        "default_content": "Consultar estado del pedido",
        "orden": 90,
    },
    # -- EstadoPreFlujo --
    {
        "message_key": "pre_flujo_message",
        "message_type": "text",
        "state_name": "EstadoPreFlujo",
        "flujo_identificacion_mensaje": "preflujoMensaje",
        "label": "Mensaje de empatía pre-flujo",
        "default_content": "Entiendo que acceder a la Plataforma puede ser complicado, pero te aseguramos que vas a tener buenos beneficios. Mientras esperas que te atendamos, podés ir probando estos pasos.",
        "orden": 10,
    },
    {
        "message_key": "preflujo_continuar_button",
        "message_type": "button",
        "state_name": "EstadoPreFlujo",
        "flujo_identificacion_mensaje": "preflujoContinuar",
        "label": "Botón Continuar",
        "default_content": "CONTINUAR",
        "orden": 20,
    },
    # -- EstadoNoVeoDescuentos --
    {
        "message_key": "descuentos_not_loaded_text",
        "message_type": "text",
        "state_name": "EstadoNoVeoDescuentos",
        "flujo_identificacion_mensaje": "descuentosFormularioNoCargado",
        "label": "Indicación de formulario no cargado",
        "default_content": "Hay un formulario en la publicación del beneficio que tenés que completar. Una vez hecho eso, esperá 48 horas y volvé a intentar. Si seguís sin ver los descuentos, avísame y te derivo con soporte.",
        "orden": 10,
    },
    {
        "message_key": "descuentos_wait_48_text",
        "message_type": "text",
        "state_name": "EstadoNoVeoDescuentos",
        "flujo_identificacion_mensaje": "descuentosEsperar48hs",
        "label": "Indicación de esperar 48hs",
        "default_content": "Perfecto. En ese caso hay que esperar 48 horas para que se procese el registro. Una vez que pase ese tiempo, probá de nuevo. 😊",
        "orden": 20,
    },
    {
        "message_key": "descuentos_login_steps_text",
        "message_type": "text",
        "state_name": "EstadoNoVeoDescuentos",
        "flujo_identificacion_mensaje": "descuentosPasosLogin",
        "label": "Pasos de inicio de sesión",
        "default_content": (
            "Como ya pasaron más de 48 horas, probá iniciando sesión.\n"
            "\n"
            "Hacé click en 'RECIBIR CÓDIGO DE ACCESO POR E-MAIL'. Ingresá tu dirección de correo electrónico y hacé click en 'ENVIAR'.\n"
            "\n"
            "Vas a recibir un código en tu mail. Volvé a la página e ingresalo."
        ),
        "orden": 30,
    },
    {
        "message_key": "descuentos_form_confusion_handoff_text",
        "message_type": "text",
        "state_name": "EstadoNoVeoDescuentos",
        "flujo_identificacion_mensaje": "descuentosConfusionFormulario",
        "label": "Derivación por confusión con el formulario",
        "default_content": "Hay un formulario de registro en la publicación del beneficio que necesitás completar para ver los descuentos. Te voy a derivar con una persona más calificada para que pueda ayudarte.",
        "orden": 40,
    },
    {
        "message_key": "descuentos_resolved_text",
        "message_type": "text",
        "state_name": "EstadoNoVeoDescuentos",
        "flujo_identificacion_mensaje": "descuentosResuelto",
        "label": "Cierre del flujo de descuentos",
        "default_content": "El flujo de descuentos ya quedó resuelto.",
        "orden": 50,
    },
    # -- EstadoFormulario --
    {
        "message_key": "formulario_login_steps_text",
        "message_type": "text",
        "state_name": "EstadoFormulario",
        "flujo_identificacion_mensaje": "formularioPasosLogin",
        "label": "Pasos de inicio de sesión",
        "default_content": (
            "Como ya pasaron más de 48 horas, probá iniciando sesión.\n"
            "\n"
            "Hacé click en 'RECIBIR CÓDIGO DE ACCESO POR E-MAIL'."
        ),
        "orden": 10,
    },
    {
        "message_key": "formulario_confusion_handoff_text",
        "message_type": "text",
        "state_name": "EstadoFormulario",
        "flujo_identificacion_mensaje": "formularioConfusion",
        "label": "Derivación por confusión con el formulario",
        "default_content": "Hay un formulario de registro en la página que necesitás completar para poder avanzar. Te voy a derivar con una persona más calificada para que pueda ayudarte.",
        "orden": 20,
    },
    {
        "message_key": "formulario_resolved_text",
        "message_type": "text",
        "state_name": "EstadoFormulario",
        "flujo_identificacion_mensaje": "formularioResuelto",
        "label": "Cierre del flujo de formulario",
        "default_content": "El flujo de formulario ya quedó resuelto.",
        "orden": 30,
    },
    # -- EstadoInfoPedido --
    {
        "message_key": "info_pedido_anything_else_text",
        "message_type": "text",
        "state_name": "EstadoInfoPedido",
        "flujo_identificacion_mensaje": "pedidoAlgoMas",
        "label": "Ofrecimiento de más ayuda",
        "default_content": "Si necesitás algo más, elegí una opción. Si no, podés cerrar la conversación.",
        "orden": 10,
    },
    # -- EstadoPedirMail --
    {
        "message_key": "pedir_mail_empresa_ok_text",
        "message_type": "text",
        "state_name": "EstadoPedirMail",
        "flujo_identificacion_mensaje": "mailEmpresarialOk",
        "label": "Mail empresarial válido",
        "default_content": "El mail parece ser correcto y es un mail empresarial. Como no veo nada incorrecto en el registro, probá nuevamente y decime si te funcionó.",
        "orden": 10,
    },
    # -- EstadoRegistroMailEmpresa --
    {
        "message_key": "registro_mail_empresa_ask_progress_text",
        "message_type": "text",
        "state_name": "EstadoRegistroMailEmpresa",
        "flujo_identificacion_mensaje": "registroAvanceMail",
        "label": "Pregunta de avance con el mail",
        "default_content": "Contame si pudiste avanzar con ese mail.",
        "orden": 10,
    },
    # -- EstadoLogin --
    {
        "message_key": "login_steps_intro_text",
        "message_type": "text",
        "state_name": "EstadoLogin",
        "flujo_identificacion_mensaje": "loginPasosDetallados",
        "label": "Pasos detallados de inicio de sesión",
        "default_content": (
            "Si ya recibiste el mail de confirmación con el acceso a la Plataforma, hacé click en \"RECIBIR CÓDIGO DE ACCESO POR E-MAIL\".\n"
            "Ingresá tu dirección de correo electrónico, y hacé click en \"ENVIAR\".\n"
            "\n"
            "Vas a recibir una clave numérica en tu mail. Volvé a la página e ingresalo.\n"
            "\n"
            "Deberías acceder sin problemas"
        ),
        "orden": 10,
    },
    # -- EstadoPasosInicioSesion --
    {
        "message_key": "pasos_step1_text",
        "message_type": "text",
        "state_name": "EstadoPasosInicioSesion",
        "flujo_identificacion_mensaje": "pasosRecibirCodigo",
        "label": "Paso 1: recibir código",
        "default_content": "Hacé click en 'RECIBIR CÓDIGO DE ACCESO POR E-MAIL'.",
        "orden": 10,
    },
    {
        "message_key": "pasos_step2_text",
        "message_type": "text",
        "state_name": "EstadoPasosInicioSesion",
        "flujo_identificacion_mensaje": "pasosIngresarEmail",
        "label": "Paso 2: ingresar email",
        "default_content": "Ingresá tu dirección de correo electrónico y hacé click en 'ENVIAR'.",
        "orden": 20,
    },
    {
        "message_key": "pasos_step3_text",
        "message_type": "text",
        "state_name": "EstadoPasosInicioSesion",
        "flujo_identificacion_mensaje": "pasosIngresarCodigo",
        "label": "Paso 3: ingresar código",
        "default_content": "Vas a recibir un código en tu mail corporativo. Volvé a la página e ingresalo. 📧",
        "orden": 30,
    },
    {
        "message_key": "pasos_ask_worked_text",
        "message_type": "text",
        "state_name": "EstadoPasosInicioSesion",
        "flujo_identificacion_mensaje": "pasosFunciono",
        "label": "Pregunta si funcionó",
        "default_content": "Contame si funcionó.",
        "orden": 40,
    },
    # -- EstadoPortalBeneficios --
    {
        "message_key": "portal_beneficios_clear_nav_text",
        "message_type": "text",
        "state_name": "EstadoPortalBeneficios",
        "flujo_identificacion_mensaje": "portalBorrarNavegacion",
        "label": "Propuesta de borrar navegación",
        "default_content": "Lo mejor en este caso es borrar los datos de navegación para asegurarnos de que salga bien. Te parece? 🧹",
        "orden": 10,
    },
    {
        "message_key": "portal_beneficios_ask_logged_in_fallback_text",
        "message_type": "text",
        "state_name": "EstadoPortalBeneficios",
        "flujo_identificacion_mensaje": "portalPreguntaInicioSesion",
        "label": "Pregunta si inició sesión (fallback)",
        "default_content": "Contame si ya iniciaste sesión en el portal de beneficios.",
        "orden": 20,
    },
    # -- EstadoBorrarNavegacion --
    {
        "message_key": "borrar_nav_confirm_text",
        "message_type": "text",
        "state_name": "EstadoBorrarNavegacion",
        "flujo_identificacion_mensaje": "borrarNavConfirmar",
        "label": "Confirmación para borrar navegación",
        "default_content": "En ese caso, borremos los datos de navegación del navegador para volver a intentarlo. Te parece?",
        "orden": 10,
    },
    {
        "message_key": "borrar_nav_explain_why_text",
        "message_type": "text",
        "state_name": "EstadoBorrarNavegacion",
        "flujo_identificacion_mensaje": "borrarNavExplicar",
        "label": "Explicación del borrado de navegación",
        "default_content": "Te cuento por qué te lo pido! A veces el navegador guarda credenciales viejas o incorrectas del portal de beneficios, y eso puede ser justo lo que está causando el problema. Borrando esos datos le damos un reinicio limpio y lo más probable es que todo funcione de una. Sabés cómo hacerlo?",
        "orden": 20,
    },
    {
        "message_key": "borrar_nav_ask_know_how_text",
        "message_type": "text",
        "state_name": "EstadoBorrarNavegacion",
        "flujo_identificacion_mensaje": "borrarNavSaberComo",
        "label": "Pregunta si sabe cómo borrar",
        "default_content": "Sabés cómo hacerlo?",
        "orden": 30,
    },
    {
        "message_key": "borrar_nav_how_to_text",
        "message_type": "text",
        "state_name": "EstadoBorrarNavegacion",
        "flujo_identificacion_mensaje": "borrarNavComoHacerlo",
        "label": "Instrucciones para borrar datos",
        "default_content": (
            "Abrí Chrome y tocá en los tres puntos (arriba a la derecha).\n"
            "Seleccioná \"Historial\" y luego \"Borrar datos de navegación\".\n"
            "Elegí el intervalo de tiempo y marcá los datos a eliminar.\n"
            "Tocá en \"Borrar datos\".\n"
            "\n"
            "Avisame cuando termines."
        ),
        "orden": 40,
    },
    {
        "message_key": "borrar_nav_wait_finish_text",
        "message_type": "text",
        "state_name": "EstadoBorrarNavegacion",
        "flujo_identificacion_mensaje": "borrarNavEsperar",
        "label": "Espera a que termine el borrado",
        "default_content": "Perfecto, avisame cuando termines.",
        "orden": 50,
    },
    {
        "message_key": "borrar_nav_ask_finished_text",
        "message_type": "text",
        "state_name": "EstadoBorrarNavegacion",
        "flujo_identificacion_mensaje": "borrarNavTerminaste",
        "label": "Pregunta si ya terminó",
        "default_content": "Avisame cuando lo termines.",
        "orden": 60,
    },
    {
        "message_key": "borrar_nav_wait_again_text",
        "message_type": "text",
        "state_name": "EstadoBorrarNavegacion",
        "flujo_identificacion_mensaje": "borrarNavRecordatorio",
        "label": "Recordatorio de espera",
        "default_content": "Cuando termines, avisame y seguimos.",
        "orden": 70,
    },
    {
        "message_key": "borrar_nav_try_again_text",
        "message_type": "text",
        "state_name": "EstadoBorrarNavegacion",
        "flujo_identificacion_mensaje": "borrarNavProbar",
        "label": "Invitación a probar de nuevo",
        "default_content": "Perfecto. Probá de nuevo y contame si funcionó.",
        "orden": 80,
    },
    {
        "message_key": "borrar_nav_ask_agree_text",
        "message_type": "text",
        "state_name": "EstadoBorrarNavegacion",
        "flujo_identificacion_mensaje": "borrarNavDeAcuerdo",
        "label": "Pregunta si está de acuerdo",
        "default_content": "Contame si te parece bien que borremos los datos de navegación.",
        "orden": 90,
    },
    {
        "message_key": "borrar_nav_ask_know_how_fallback_text",
        "message_type": "text",
        "state_name": "EstadoBorrarNavegacion",
        "flujo_identificacion_mensaje": "borrarNavSaberComoFallback",
        "label": "Pregunta si sabe cómo borrar (fallback)",
        "default_content": "Contame si sabés cómo hacerlo.",
        "orden": 100,
    },
    {
        "message_key": "borrar_nav_registro_code_text",
        "message_type": "text",
        "state_name": "EstadoBorrarNavegacion",
        "flujo_identificacion_mensaje": "borrarNavCodigoRegistro",
        "label": "Indicación para ingresar con código",
        "default_content": "Ingresá de nuevo, hacé click en registro, ingresá tu mail y usá este código:",
        "orden": 110,
    },
    {
        "message_key": "borrar_nav_steps_done_text",
        "message_type": "text",
        "state_name": "EstadoBorrarNavegacion",
        "flujo_identificacion_mensaje": "borrarNavPasosIndicados",
        "label": "Aviso de pasos ya indicados",
        "default_content": "Ya te indiqué los pasos para borrar navegación.",
        "orden": 120,
    },
    # -- EstadoSoporte --
    {
        "message_key": "soporte_handoff_text",
        "message_type": "text",
        "state_name": "EstadoSoporte",
        "flujo_identificacion_mensaje": "soporteDerivacion",
        "label": "Mensaje de derivación a soporte",
        "default_content": "Perdón, no estoy pudiendo solucionar tu problema. Te voy a derivar con una persona más calificada que te va a ayudar. 🤝",
        "orden": 10,
    },
]

# ==============================
# Cliente BigQuery
# ==============================

_BIGQUERY_CLIENT = None
_CLIENT_LOCK = Lock()


def _bigquery_configured() -> bool:
    return bigquery is not None and bool(os.getenv("GOOGLE_CLOUD_PROJECT"))


def _table_id(client, table_name: Optional[str] = None) -> str:
    name = table_name or BIGQUERY_BOT_MESSAGES_TABLE
    return f"{client.project}.{BIGQUERY_DATASET}.{name}"


def _ensure_table(client) -> None:
    dataset_id = f"{client.project}.{BIGQUERY_DATASET}"
    dataset = bigquery.Dataset(dataset_id)
    dataset.location = os.getenv("BIGQUERY_CLOUD_LOCATION", "us-central1")
    client.create_dataset(dataset, exists_ok=True)

    schema = [
        bigquery.SchemaField("message_key", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("message_type", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("state_name", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("flujo_identificacion_mensaje", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("label", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("content", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("default_content", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("orden", "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("updated_at", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("updated_by", "STRING", mode="NULLABLE"),
    ]
    table = bigquery.Table(f"{dataset_id}.{BIGQUERY_BOT_MESSAGES_TABLE}", schema=schema)
    client.create_table(table, exists_ok=True)

    _migrate_from_legacy(client)
    _seed_if_empty(client)


def _legacy_table_exists(client) -> bool:
    dataset_id = f"{client.project}.{BIGQUERY_DATASET}"
    table_ref = f"{dataset_id}.{LEGACY_BOT_MESSAGES_TABLE}"
    try:
        client.get_table(table_ref)
        return True
    except Exception:
        return False


def _migrate_from_legacy(client) -> None:
    """Migrates latest rows from bot_messages to mensajes_editables, then drops old table."""
    if not _legacy_table_exists(client):
        return

    legacy_table_id = _table_id(client, LEGACY_BOT_MESSAGES_TABLE)
    new_table_id = _table_id(client, BIGQUERY_BOT_MESSAGES_TABLE)

    # Read latest row per message_key from legacy table
    query = f"""
        SELECT message_key, message_type, state_name, label, content, default_content, updated_at, updated_by
        FROM `{legacy_table_id}`
        QUALIFY ROW_NUMBER() OVER (PARTITION BY message_key ORDER BY updated_at DESC) = 1
    """
    try:
        rows = list(client.query(query).result())
    except Exception as e:
        logger.warning(f"[message-store] no se pudo leer tabla legacy {legacy_table_id}: {e}")
        return

    if not rows:
        logger.info("[message-store] tabla legacy vacia, nada que migrar")
        _drop_legacy_table(client)
        return

    # Build default metadata mapping to preserve flujo_identificacion_mensaje and orden
    default_meta = {m["message_key"]: m for m in DEFAULT_MESSAGES}

    now = datetime.now(timezone.utc).isoformat()
    migrated = []
    for row in rows:
        key = row["message_key"]
        meta = default_meta.get(key, {})
        migrated.append({
            "message_key": key,
            "message_type": row["message_type"],
            "state_name": row["state_name"],
            "flujo_identificacion_mensaje": meta.get("flujo_identificacion_mensaje"),
            "label": row["label"],
            "content": row["content"],
            "default_content": row["default_content"],
            "orden": meta.get("orden"),
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else now,
            "updated_by": row["updated_by"] or "migracion-legacy",
        })

    errors = client.insert_rows_json(new_table_id, migrated)
    if errors:
        logger.error(f"[message-store] error migrando desde tabla legacy: {errors}")
        return

    logger.info(f"[message-store] migradas {len(migrated)} filas desde {legacy_table_id} a {new_table_id}")
    _drop_legacy_table(client)


def _drop_legacy_table(client) -> None:
    legacy_table_id = _table_id(client, LEGACY_BOT_MESSAGES_TABLE)
    try:
        client.delete_table(legacy_table_id, not_found_ok=True)
        logger.info(f"[message-store] tabla legacy {legacy_table_id} eliminada")
    except Exception as e:
        logger.warning(f"[message-store] no se pudo eliminar tabla legacy {legacy_table_id}: {e}")


def _seed_if_empty(client) -> None:
    table_id = _table_id(client)
    rows = list(client.query(f"SELECT COUNT(*) AS c FROM `{table_id}`").result())
    if rows and rows[0]["c"]:
        return

    now = datetime.now(timezone.utc).isoformat()
    seed_rows = [
        {
            "message_key": m["message_key"],
            "message_type": m["message_type"],
            "state_name": m["state_name"],
            "flujo_identificacion_mensaje": m.get("flujo_identificacion_mensaje"),
            "label": m["label"],
            "content": m["default_content"],
            "default_content": m["default_content"],
            "orden": m.get("orden"),
            "updated_at": now,
            "updated_by": "system-seed",
        }
        for m in DEFAULT_MESSAGES
    ]
    errors = client.insert_rows_json(table_id, seed_rows)
    if errors:
        logger.error(f"[message-store] error pre-poblando mensajes_editables: {errors}")
    else:
        logger.info(f"[message-store] tabla mensajes_editables pre-poblada con {len(seed_rows)} defaults")


def _get_client():
    global _BIGQUERY_CLIENT

    if not _bigquery_configured():
        raise RuntimeError(
            "BigQuery no esta configurado (falta google-cloud-bigquery o GOOGLE_CLOUD_PROJECT)"
        )

    with _CLIENT_LOCK:
        if _BIGQUERY_CLIENT is None:
            client = bigquery.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"))
            _ensure_table(client)
            _BIGQUERY_CLIENT = client
        return _BIGQUERY_CLIENT


def _load_from_bigquery() -> List[Dict[str, Any]]:
    client = _get_client()
    query = f"""
        SELECT message_key, message_type, state_name, flujo_identificacion_mensaje,
               label, content, default_content, orden, updated_at, updated_by
        FROM `{_table_id(client)}`
        QUALIFY ROW_NUMBER() OVER (PARTITION BY message_key ORDER BY updated_at DESC) = 1
    """
    rows = client.query(query).result()
    records = []
    for row in rows:
        records.append({
            "message_key": row["message_key"],
            "message_type": row["message_type"],
            "state_name": row["state_name"],
            "flujo_identificacion_mensaje": row["flujo_identificacion_mensaje"],
            "label": row["label"],
            "content": row["content"],
            "default_content": row["default_content"],
            "orden": row["orden"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "updated_by": row["updated_by"],
        })
    return records


# ==============================
# Cache en memoria
# ==============================

_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LOCK = Lock()
_CACHE_LOADED_AT: Optional[float] = None


def _default_records() -> List[Dict[str, Any]]:
    return [
        {
            "message_key": m["message_key"],
            "message_type": m["message_type"],
            "state_name": m["state_name"],
            "flujo_identificacion_mensaje": m.get("flujo_identificacion_mensaje"),
            "label": m["label"],
            "content": m["default_content"],
            "default_content": m["default_content"],
            "orden": m.get("orden"),
            "updated_at": None,
            "updated_by": None,
        }
        for m in DEFAULT_MESSAGES
    ]


def _populate_cache() -> None:
    global _CACHE_LOADED_AT

    records: Optional[List[Dict[str, Any]]] = None
    source = "default"

    if _bigquery_configured():
        try:
            records = _load_from_bigquery()
            source = "bigquery"
        except Exception as e:
            logger.warning(f"[message-store] BigQuery no disponible, usando defaults: {e}")
            records = None

    if records is None:
        records = _default_records()
        source = "default"

    with _CACHE_LOCK:
        _CACHE.clear()
        for record in records:
            entry = dict(record)
            entry["source"] = source
            _CACHE[entry["message_key"]] = entry
        _CACHE_LOADED_AT = time.monotonic()

    logger.info(f"[message-store] cache cargado con {len(records)} mensajes")


def _ensure_cache_fresh() -> None:
    with _CACHE_LOCK:
        loaded_at = _CACHE_LOADED_AT
        is_empty = not _CACHE

    needs_reload = is_empty or loaded_at is None or (time.monotonic() - loaded_at) > CACHE_TTL_SECONDS
    if needs_reload:
        _populate_cache()


# ==============================
# Interfaz publica
# ==============================

def get_message(key: str, default: str = "") -> str:
    _ensure_cache_fresh()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    return entry["content"] if entry else default


def get_message_metadata(key: str) -> Optional[Dict[str, Any]]:
    _ensure_cache_fresh()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    return dict(entry) if entry else None


def load_all_messages() -> Dict[str, str]:
    _ensure_cache_fresh()
    with _CACHE_LOCK:
        return {key: entry["content"] for key, entry in _CACHE.items()}


def get_all_messages_with_metadata() -> List[Dict[str, Any]]:
    _ensure_cache_fresh()
    with _CACHE_LOCK:
        items = [dict(entry) for entry in _CACHE.values()]
    items.sort(key=lambda m: (
        m.get("state_name") is not None,
        m.get("state_name") or "",
        m.get("orden") if m.get("orden") is not None else 999999,
        m["message_key"],
    ))
    return items


def set_message(key: str, content: str, updated_by: str = "") -> None:
    meta = get_message_metadata(key)
    if meta is None:
        raise KeyError(key)

    client = _get_client()
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "message_key": key,
        "message_type": meta["message_type"],
        "state_name": meta.get("state_name"),
        "flujo_identificacion_mensaje": meta.get("flujo_identificacion_mensaje"),
        "label": meta.get("label"),
        "content": content,
        "default_content": meta["default_content"],
        "orden": meta.get("orden"),
        "updated_at": now,
        "updated_by": updated_by or "",
    }
    errors = client.insert_rows_json(_table_id(client), [row])
    if errors:
        raise RuntimeError(str(errors))

    invalidate_cache()


def create_message(
    message_key: str,
    message_type: str,
    state_name: Optional[str],
    label: Optional[str],
    content: str,
    default_content: Optional[str] = None,
    updated_by: str = "",
    flujo_identificacion_mensaje: Optional[str] = None,
    orden: Optional[int] = None,
) -> None:
    if get_message_metadata(message_key) is not None:
        raise ValueError(f"message_key '{message_key}' ya existe")

    if message_type not in MESSAGE_LIMITS:
        raise ValueError(f"message_type '{message_type}' no es valido")

    trimmed = content.strip()
    if not trimmed:
        raise ValueError("content no puede estar vacio")

    limit = MESSAGE_LIMITS.get(message_type)
    if limit and len(trimmed) > limit:
        raise ValueError(f"content excede el limite de {limit} caracteres para {message_type}")

    resolved_default = (default_content or trimmed).strip()
    if not resolved_default:
        raise ValueError("default_content no puede estar vacio")

    # Auto-assign next order within the same state_name if not provided
    resolved_orden = orden
    if resolved_orden is None:
        items = get_all_messages_with_metadata()
        same_state = [m for m in items if m.get("state_name") == state_name and m.get("orden") is not None]
        if same_state:
            resolved_orden = max(m["orden"] for m in same_state) + 10
        else:
            resolved_orden = 10

    client = _get_client()
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "message_key": message_key,
        "message_type": message_type,
        "state_name": state_name,
        "flujo_identificacion_mensaje": flujo_identificacion_mensaje,
        "label": label,
        "content": trimmed,
        "default_content": resolved_default,
        "orden": resolved_orden,
        "updated_at": now,
        "updated_by": updated_by or "",
    }
    errors = client.insert_rows_json(_table_id(client), [row])
    if errors:
        raise RuntimeError(str(errors))

    invalidate_cache()


def update_message_order(orders: List[Dict[str, Any]]) -> None:
    """Update orden for a list of messages.

    orders: list of dicts with {"message_key": str, "orden": int}
    """
    if not orders:
        return

    # Validate keys and collect metadata before touching BigQuery.
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for item in orders:
        key = item.get("message_key")
        new_orden = item.get("orden")
        if key is None or new_orden is None:
            continue
        meta = get_message_metadata(key)
        if meta is None:
            raise KeyError(key)
        rows.append({
            "message_key": key,
            "message_type": meta["message_type"],
            "state_name": meta.get("state_name"),
            "flujo_identificacion_mensaje": meta.get("flujo_identificacion_mensaje"),
            "label": meta.get("label"),
            "content": meta["content"],
            "default_content": meta["default_content"],
            "orden": new_orden,
            "updated_at": now,
            "updated_by": "system-reorder",
        })

    if not rows:
        return

    client = _get_client()
    table_id = _table_id(client)
    errors = client.insert_rows_json(table_id, rows)
    if errors:
        raise RuntimeError(str(errors))
    invalidate_cache()


def invalidate_cache() -> None:
    global _CACHE_LOADED_AT
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE_LOADED_AT = None
    logger.info("[message-store] cache invalidado")
