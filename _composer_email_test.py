"""One-off: send rtalukdar2002@gmail.com a single test email showing how the
workout composer parses nine session titles. Uses the app's SMTP config and the
fiT-X email design tokens. No database involved.
"""
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

os.environ["DATABASE_URL"] = ""  # keep this run away from any database
import sys
sys.path.insert(0, ".")

from app.config import Config
from app.plans import build_plan

TO = "rtalukdar2002@gmail.com"
TITLES = [
    "schedule a workout of boxing",
    "schedule a workout of running",
    "schedule a workout of push day",
    "schedule a workout of pull day",
    "schedule a workout of back bicep",
    "schedule a workout of chest tricep",
    "schedule a workout of calesthenics",
    "schedule a workout of home workout - upper body",
    "schedule a workout of home workout - lower body",
]

FONT = "'Segoe UI','Helvetica Neue',Helvetica,Arial,sans-serif"
MONO = "'JetBrains Mono','Consolas',monospace"

results = []
for t in TITLES:
    plan = build_plan(t)
    blocks = []
    if plan:
        for b in plan["blocks"]:
            rows = "".join(
                f"""<tr>
                  <td style="padding:11px 20px;{' border-top:1px solid #262626;' if i else ''} font-family:{FONT}; font-size:14px; font-weight:600; color:#F5F5F4;">{e['name']}</td>
                  <td align="right" style="padding:11px 20px;{' border-top:1px solid #262626;' if i else ''} font-family:{MONO}; font-size:12px; font-weight:700; color:#4ADE80; white-space:nowrap;">{e['dose']}</td>
                </tr>"""
                for i, e in enumerate(b["exercises"])
            )
            blocks.append(f"""
            <div style="font-family:{MONO}; font-size:10px; font-weight:700; letter-spacing:2.5px; color:#4ADE80; padding:12px 0 4px;">{b['block'].upper()} <span style="color:#9CA3AF; letter-spacing:0;">— {b['focus']}</span></div>
            <table role="presentation" width="100%" bgcolor="#151515" cellpadding="0" cellspacing="0" style="background-color:#151515; border:1px solid #262626; border-radius:14px;">{rows}</table>""")
        body = (f'<div style="font-family:{FONT}; font-size:13px; color:#9CA3AF; padding-bottom:10px;">parsed as <span style="color:#4ADE80; font-weight:700;">{plan["name"]}</span> · {sum(len(b["exercises"]) for b in plan["blocks"])} exercises</div>' + "".join(blocks))
    else:
        body = """<table role="presentation" width="100%" bgcolor="#151515" cellpadding="0" cellspacing="0" style="background-color:#151515; border:1px dashed #262626; border-radius:14px;">
          <tr><td style="padding:16px 20px; font-family:'Segoe UI',Arial,sans-serif; font-size:14px; color:#9CA3AF;">No plan matched — the reminder email renders without a session plan section.</td></tr>
        </table>"""
    results.append((t, plan, body))

def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

sections = []
for i, (t, plan, body) in enumerate(results):
    sections.append(f"""
    <tr><td style="padding:{'0' if i == 0 else '34px'} 32px 0;">
      <div style="font-family:{MONO}; font-size:11px; letter-spacing:2px; color:#9CA3AF; padding-bottom:8px;">TEST {i + 1} · <span style="color:#FEF08A;">"{esc(t)}"</span></div>
      {body}
    </td></tr>""")

html = f"""<!DOCTYPE html>
<html><body style="margin:0; padding:0; background-color:#0A0A0A;">
<table role="presentation" width="100%" bgcolor="#0A0A0A" cellpadding="0" cellspacing="0">
  <tr><td align="center" style="padding:36px 0;">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px; max-width:600px;">
      <tr><td style="padding:0 32px;">
        <div style="height:4px; width:44px; background:#4ADE80; border-radius:2px;"></div>
        <div style="font-family:{FONT}; font-size:21px; font-weight:800; letter-spacing:-0.4px; color:#F5F5F4; padding-top:14px;">fi<span style="color:#4ADE80;">T-X</span></div>
        <div style="font-family:{FONT}; font-size:11px; font-weight:700; letter-spacing:3px; color:#4ADE80; padding-top:26px;">COMPOSER TEST</div>
        <div style="font-family:{FONT}; font-size:32px; font-weight:800; letter-spacing:-1px; color:#F5F5F4; padding-top:8px;">9 titles in.<br><span style="color:#FEF08A;">what comes out.</span></div>
        <div style="font-family:{FONT}; font-size:14px; line-height:22px; color:#9CA3AF; padding-top:14px;">Every title below was run through the session-plan composer exactly as typed. This is what a reminder email would attach for each one.</div>
      </td></tr>
      {''.join(sections)}
      <tr><td style="padding:36px 32px 12px; font-family:{FONT}; font-size:12px; line-height:19px; color:#9CA3AF;">
        Plans are composed from the bundled exercise library — same title, same plan. Unrecognized titles fall back to no plan section.
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""

plain_lines = ["fiT-X composer test — 9 titles\n"]
for i, (t, plan, _) in enumerate(results):
    plain_lines.append(f"\nTEST {i+1}: \"{t}\"")
    if plan:
        plain_lines.append(f"  parsed as: {plan['name']}")
        for b in plan["blocks"]:
            plain_lines.append(f"  {b['block'].upper()} — {b['focus']}")
            plain_lines.extend(f"    - {e['name']} · {e['dose']}" for e in b["exercises"])
    else:
        plain_lines.append("  no plan matched (email renders without a session plan)")
plain = "\n".join(plain_lines)

msg = EmailMessage()
msg["From"] = formataddr(("fiT-X Coach", Config.SMTP_FROM or Config.SMTP_USER))
msg["To"] = TO
msg["Subject"] = "fiT-X · Composer test · 9 session titles"
msg.set_content(plain)
msg.add_alternative(html, subtype="html")

os.makedirs("previews", exist_ok=True)
with open("previews/composer_test.html", "w", encoding="utf-8") as f:
    f.write(html)

try:
    with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(Config.SMTP_USER, Config.SMTP_APP_PASSWORD)
        server.send_message(msg)
    print(f"SENT to {TO} via {Config.SMTP_HOST}:{Config.SMTP_PORT}")
    print(f"from: {Config.SMTP_FROM or Config.SMTP_USER}")
except Exception as exc:
    print(f"SEND FAILED: {exc}")
    print("rendered email saved to previews/composer_test.html")

print("\nparse summary:")
for i, (t, plan, _) in enumerate(results):
    print(f"  {i+1}. {t!r} -> {plan['name'] if plan else 'NO PLAN'}")
