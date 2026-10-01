// Passkey ceremonies for the approval pages. No inline script: the CSP forbids it.
"use strict";

const b64urlToBuf = (value) => {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0)).buffer;
};

const bufToB64url = (buffer) =>
  btoa(String.fromCharCode(...new Uint8Array(buffer)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function credentialToJSON(credential) {
  const r = credential.response;
  const out = {
    id: credential.id,
    rawId: bufToB64url(credential.rawId),
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: { clientDataJSON: bufToB64url(r.clientDataJSON) },
  };
  if (credential.authenticatorAttachment) out.authenticatorAttachment = credential.authenticatorAttachment;
  if (r.attestationObject) {
    out.response.attestationObject = bufToB64url(r.attestationObject);
    if (r.getTransports) out.response.transports = r.getTransports();
  }
  if (r.authenticatorData) {
    out.response.authenticatorData = bufToB64url(r.authenticatorData);
    out.response.signature = bufToB64url(r.signature);
    if (r.userHandle) out.response.userHandle = bufToB64url(r.userHandle);
  }
  return out;
}

function show(element, message, ok) {
  element.textContent = message;
  element.className = "result " + (ok ? "ok" : "bad");
}

function setupDecide() {
  const section = document.getElementById("decide");
  if (!section || !section.dataset.changeId) return;
  const id = section.dataset.changeId;
  const result = document.getElementById("decide-result");
  const buttons = section.querySelectorAll("[data-decide]");
  buttons.forEach((button) =>
    button.addEventListener("click", async () => {
      const action = button.dataset.decide;
      buttons.forEach((b) => (b.disabled = true));
      show(result, "Waiting for your passkey…", true);
      try {
        const options = await postJSON(`/changes/${id}/options`, { action });
        options.challenge = b64urlToBuf(options.challenge);
        options.allowCredentials = (options.allowCredentials || []).map((c) => ({ ...c, id: b64urlToBuf(c.id) }));
        const credential = await navigator.credentials.get({ publicKey: options });
        const data = await postJSON(`/changes/${id}/decide`, { action, credential: credentialToJSON(credential) });
        show(result, data.status === "approved" ? "Approved. A bot can apply it now." : "Rejected.", true);
        setTimeout(() => window.location.reload(), 1200);
      } catch (err) {
        show(result, "Not recorded: " + err.message, false);
        buttons.forEach((b) => (b.disabled = false));
      }
    })
  );
}

function setupRegister() {
  const section = document.getElementById("register");
  if (!section) return;
  const invite = section.dataset.invite;
  const button = document.getElementById("register-button");
  const result = document.getElementById("register-result");
  button.addEventListener("click", async () => {
    button.disabled = true;
    show(result, "Follow your device's prompt…", true);
    try {
      const options = await postJSON("/passkeys/register/options", { invite });
      options.challenge = b64urlToBuf(options.challenge);
      options.user.id = b64urlToBuf(options.user.id);
      options.excludeCredentials = (options.excludeCredentials || []).map((c) => ({ ...c, id: b64urlToBuf(c.id) }));
      const credential = await navigator.credentials.create({ publicKey: options });
      const label = document.getElementById("label").value;
      const data = await postJSON("/passkeys/register/verify", { invite, label, credential: credentialToJSON(credential) });
      show(result, `Passkey “${data.label}” added. You can close this page.`, true);
    } catch (err) {
      show(result, "Not added: " + err.message, false);
      button.disabled = false;
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupDecide();
  setupRegister();
});
