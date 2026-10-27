"""
Every sentence on the port-80 Wi-Fi page (install plan §4), English and Spanish.

``en`` and ``es`` hold the same keys; ``tests/test_setup_page.py`` fails the
build if they drift. The sentences come from the install spec
(``marketplace/docs/operations/wqm1-install-spec.md``: B3, B7, C3, C4, C6,
C7, D3–D7). Customer-facing words go past AIR and MARKET before the Captain
merges a change here.

Reason codes are the ``utils.netctl._STATION_FAIL`` kinds plus
``no_bluesignal`` and ``interrupted``. The page shows the sentence for the
phone's language; it never builds a sentence from parts it was not given.
"""

from __future__ import annotations

en: dict[str, str] = {
    "lang": "en",
    "title": "Set up Wi-Fi",
    "lead": "Pick the home Wi-Fi and type its password.",
    "stay_connected": "If your phone asks, tap Keep or Stay connected.",
    "networks": "Wi-Fi networks",
    "other": "Other network (type its name)",
    "name_label": "Network name",
    "password_label": "Password",  # nosec B105 - a sentence
    "show_password": "Show password",  # nosec B105 - a sentence
    "keep_space": "The password really ends with a space",
    "connect": "Connect",
    "rescan": "Rescan",
    "rescanning": "Looking for networks…",
    "rescan_failed": "Couldn't refresh the list. Type the network name if yours isn't here.",
    "none_found": "No networks found yet. Tap Rescan, or type the network name.",
    "only_24": (
        "Only 2.4 GHz networks show. If yours is 5 GHz only, turn on 2.4 GHz in the router app."
    ),
    "hidden_nearby": "A hidden network is in range. Type its name.",
    "weak": "Weak here. Readings may drop. Move the router or add an extender.",
    "phone_hotspot": "This looks like a phone hotspot. The unit goes offline when you leave.",
    "open_network": (
        "This network is open. If it has a sign-in page, the unit can't use it. "
        "A network with a normal password works."
    ),
    "drop_warning": ("Your phone will leave this network when you tap Connect. Watch the lights."),
    "joining": "Connecting to {ssid}. Watch the lights.",
    "busy": "Already connecting to {ssid} — watch the lights",
    "joined": "{ssid} is connected. Finish in BlueSignal Cloud.",
    "pick_first": "Pick a network, or type its name.",
    "network_name_bad": "That network name can't be used. Pick one from the list.",
    "trailing_space": (
        "The password ends with a space. If it really does, tick the box below "
        "and tap Connect again."
    ),
    "password_needed": "Type the Wi-Fi password.",  # nosec B105 - a sentence
    "password_short": "A Wi-Fi password is 8 to 63 characters. This one is too short.",  # nosec B105 - a sentence
    "password_long": "A Wi-Fi password is 8 to 63 characters. This one is too long.",  # nosec B105 - a sentence
    "password_char": "The password has a character the unit can't type. Type it again.",  # nosec B105 - a sentence
    "refused_band": (
        "That network is 5 GHz. The unit only uses 2.4 GHz. Turn on 2.4 GHz in the router app."
    ),
    "refused_enterprise": (
        "That network needs a work login, not just a password. The unit can't join it."
    ),
    "refused_sae_only": (
        "That network is WPA3 only. Turn on WPA2/WPA3 in the router app, or use another network."
    ),
    "refused_wep": "That network uses old WEP security. The unit can't join it.",
    "wrong_password": "The password was not accepted.",  # nosec B105 - a sentence
    "not_found": "That network wasn't found. Move the unit closer to the router.",
    "no_dhcp": (
        "Your router didn't give the unit an address. Restart the router or check its device limit."
    ),
    "no_internet": "The unit joined, but the router isn't connected to the internet.",
    "no_association": "The unit couldn't join that network. Check the password and try again.",
    "invalid_ssid": "That network name can't be used. Pick one from the list.",
    "captive_sign_in": "This network needs a sign-in page the unit can't do.",
    "no_bluesignal": (
        "Your router may be blocking new devices. Approve it in the router app, "
        "then tap Connect again."
    ),
    "interrupted": "The last try didn't finish. Tap Connect again.",
    "try_again": "Couldn't reach the unit. Check you are on BlueSignal Setup and try again.",
    "not_here": "Nothing here.",
    "open_setup": "Open the Wi-Fi setup page",
}

es: dict[str, str] = {
    "lang": "es",
    "title": "Configurar el Wi-Fi",
    "lead": "Elija el Wi-Fi de la casa y escriba su contraseña.",
    "stay_connected": "Si su teléfono pregunta, toque Mantener o Seguir conectado.",
    "networks": "Redes Wi-Fi",
    "other": "Otra red (escriba su nombre)",
    "name_label": "Nombre de la red",
    "password_label": "Contraseña",  # nosec B105 - a sentence
    "show_password": "Mostrar contraseña",  # nosec B105 - a sentence
    "keep_space": "La contraseña de verdad termina con un espacio",
    "connect": "Conectar",
    "rescan": "Buscar de nuevo",
    "rescanning": "Buscando redes…",
    "rescan_failed": ("No se pudo actualizar la lista. Si su red no aparece, escriba su nombre."),
    "none_found": "Todavía no se encontraron redes. Toque Buscar de nuevo o escriba el nombre.",
    "only_24": (
        "Solo aparecen redes de 2.4 GHz. Si la suya es solo de 5 GHz, "
        "active 2.4 GHz en la app del router."
    ),
    "hidden_nearby": "Hay una red oculta cerca. Escriba su nombre.",
    "weak": (
        "La señal es débil aquí. Las lecturas pueden fallar. "
        "Mueva el router o agregue un repetidor."
    ),
    "phone_hotspot": (
        "Parece el punto de acceso de un teléfono. La unidad se desconecta cuando usted se vaya."
    ),
    "open_network": (
        "Esta red está abierta. Si pide iniciar sesión en una página, la unidad no puede usarla. "
        "Una red con contraseña normal sí funciona."
    ),
    "drop_warning": ("Su teléfono saldrá de esta red cuando toque Conectar. Mire las luces."),
    "joining": "Conectando a {ssid}. Mire las luces.",
    "busy": "Ya se está conectando a {ssid} — mire las luces",
    "joined": "{ssid} está conectada. Termine en BlueSignal Cloud.",
    "pick_first": "Elija una red o escriba su nombre.",
    "network_name_bad": "Ese nombre de red no se puede usar. Elija una de la lista.",
    "trailing_space": (
        "La contraseña termina con un espacio. Si de verdad es así, marque la casilla "
        "de abajo y toque Conectar otra vez."
    ),
    "password_needed": "Escriba la contraseña del Wi-Fi.",  # nosec B105 - a sentence
    "password_short": (  # nosec B105 - a sentence
        "Una contraseña de Wi-Fi tiene de 8 a 63 caracteres. Esta es demasiado corta."
    ),
    "password_long": (  # nosec B105 - a sentence
        "Una contraseña de Wi-Fi tiene de 8 a 63 caracteres. Esta es demasiado larga."
    ),
    "password_char": (  # nosec B105 - a sentence
        "La contraseña tiene un carácter que la unidad no puede escribir. Escríbala otra vez."
    ),
    "refused_band": (
        "Esa red es de 5 GHz. La unidad solo usa 2.4 GHz. Active 2.4 GHz en la app del router."
    ),
    "refused_enterprise": (
        "Esa red pide un usuario de trabajo, no solo una contraseña. La unidad no puede conectarse."
    ),
    "refused_sae_only": (
        "Esa red es solo WPA3. Active WPA2/WPA3 en la app del router o use otra red."
    ),
    "refused_wep": "Esa red usa la seguridad antigua WEP. La unidad no puede conectarse.",
    "wrong_password": "La contraseña no fue aceptada.",  # nosec B105 - a sentence
    "not_found": "No se encontró esa red. Acerque la unidad al router.",
    "no_dhcp": (
        "Su router no le dio una dirección a la unidad. "
        "Reinicie el router o revise su límite de dispositivos."
    ),
    "no_internet": "La unidad se conectó, pero el router no tiene internet.",
    "no_association": (
        "La unidad no pudo conectarse a esa red. Revise la contraseña e intente otra vez."
    ),
    "invalid_ssid": "Ese nombre de red no se puede usar. Elija una de la lista.",
    "captive_sign_in": "Esta red pide iniciar sesión en una página y la unidad no puede.",
    "no_bluesignal": (
        "Su router puede estar bloqueando dispositivos nuevos. Apruebe este en la app "
        "del router y toque Conectar otra vez."
    ),
    "interrupted": "El último intento no terminó. Toque Conectar otra vez.",
    "try_again": (
        "No se pudo llegar a la unidad. Revise que está en BlueSignal Setup e intente otra vez."
    ),
    "not_here": "Aquí no hay nada.",
    "open_setup": "Abrir la página de configuración del Wi-Fi",
}

LANGUAGES = {"en": en, "es": es}


def pick_language(accept_language: str) -> str:
    """``es`` when the phone prefers Spanish over English, else ``en`` (C9)."""
    best_lang, best_q = "en", -1.0
    for part in (accept_language or "").split(","):
        piece = part.strip()
        if not piece:
            continue
        tag, _, params = piece.partition(";")
        primary = tag.strip().lower().split("-", 1)[0]
        q = 1.0
        for param in params.split(";"):
            key, _, value = param.strip().partition("=")
            if key == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        if primary in LANGUAGES and q > best_q:
            best_lang, best_q = primary, q
    return best_lang


def copy_for(accept_language: str) -> dict[str, str]:
    return LANGUAGES[pick_language(accept_language)]
