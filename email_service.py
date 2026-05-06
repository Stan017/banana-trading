import resend
import logging
from config import RESEND_API_KEY, FROM_EMAIL, LOGO_URL, APP_URL

logger = logging.getLogger(__name__)

resend.api_key = RESEND_API_KEY

def _html_bienvenida(nombre: str) -> str:
    nombre_display = nombre or "Trader"
    logo_block = (
        f'<img src="{LOGO_URL}" alt="Banana" width="72" height="72" style="display:block;margin:0 auto 12px;border-radius:16px;">'
        if LOGO_URL else
        '<div style="font-size:56px;text-align:center;margin-bottom:8px;">🍌</div>'
    )
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Bienvenido a Banana</title>
</head>
<body style="margin:0;padding:0;background:#F0EBE3;font-family:'Outfit','Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F0EBE3;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="540" cellpadding="0" cellspacing="0" style="background:#F8F4EE;border-radius:20px;border:1px solid rgba(0,0,0,0.08);overflow:hidden;">

          <!-- Header -->
          <tr>
            <td style="padding:40px 40px 28px;text-align:center;border-bottom:1px solid rgba(0,0,0,0.07);">
              {logo_block}
              <p style="margin:0 0 6px;font-size:26px;font-weight:700;color:#1A1612;letter-spacing:-0.5px;">
                Banana
              </p>
              <p style="margin:0;font-size:15px;color:#8A7A6A;font-weight:400;">
                Meet your trading partner
              </p>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:32px 40px;">
              <p style="margin:0 0 8px;font-size:20px;font-weight:600;color:#1A1612;">
                Bienvenido, {nombre_display} 👋
              </p>
              <p style="margin:0 0 24px;font-size:15px;color:#4A3F35;line-height:1.7;">
                Tu cuenta está activa. Tienes acceso a análisis institucional de mercado, journal de trades con IA y metodología cuantitativa aplicada a crypto.
              </p>

              <!-- Features -->
              <table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 28px;">
                <tr>
                  <td style="padding:13px 16px;background:#EDE6DC;border-radius:10px;border:1px solid rgba(0,0,0,0.07);">
                    <p style="margin:0;font-size:12px;color:#4A75A8;font-weight:700;letter-spacing:0.8px;text-transform:uppercase;">Análisis en tiempo real</p>
                    <p style="margin:4px 0 0;font-size:13px;color:#4A3F35;">RSI, EMAs, Funding Rate, Open Interest, scanner de confluencias HTF</p>
                  </td>
                </tr>
                <tr><td style="height:8px;"></td></tr>
                <tr>
                  <td style="padding:13px 16px;background:#EDE6DC;border-radius:10px;border:1px solid rgba(0,0,0,0.07);">
                    <p style="margin:0;font-size:12px;color:#4A75A8;font-weight:700;letter-spacing:0.8px;text-transform:uppercase;">Journal con IA</p>
                    <p style="margin:4px 0 0;font-size:13px;color:#4A3F35;">R:R, PnL perpetuos y gestión de riesgo institucional en cada trade</p>
                  </td>
                </tr>
                <tr><td style="height:8px;"></td></tr>
                <tr>
                  <td style="padding:13px 16px;background:#EDE6DC;border-radius:10px;border:1px solid rgba(0,0,0,0.07);">
                    <p style="margin:0;font-size:12px;color:#4A75A8;font-weight:700;letter-spacing:0.8px;text-transform:uppercase;">Edge Analytics</p>
                    <p style="margin:4px 0 0;font-size:13px;color:#4A3F35;">Estadísticas históricas: sesiones, CME gaps, FOMC, ciclo halving</p>
                  </td>
                </tr>
              </table>

              <!-- CTA -->
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center">
                    <a href="{APP_URL}" style="display:inline-block;background:#4A75A8;color:#ffffff;font-size:14px;font-weight:700;padding:14px 40px;border-radius:12px;text-decoration:none;letter-spacing:0.3px;">
                      Abrir Banana →
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:20px 40px;border-top:1px solid rgba(0,0,0,0.07);">
              <p style="margin:0;font-size:12px;color:#8A7A6A;text-align:center;line-height:1.6;">
                Banana — Análisis educativo, no asesoría financiera.<br>
                Si no creaste esta cuenta, ignora este email.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def enviar_bienvenida(email: str, nombre: str = None) -> bool:
    """Envía email de bienvenida. Retorna True si fue exitoso."""
    if not resend.api_key:
        logger.warning("RESEND_API_KEY no configurada — email de bienvenida omitido")
        return False
    try:
        resend.Emails.send({
            "from":    FROM_EMAIL,
            "to":      [email],
            "subject": "Bienvenido a Banana 🍌",
            "html":    _html_bienvenida(nombre),
        })
        logger.info(f"Email de bienvenida enviado a {email}")
        return True
    except Exception as e:
        logger.error(f"Error enviando email de bienvenida a {email}: {e}")
        return False
