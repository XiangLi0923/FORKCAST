let expiry;

export function clearReceipt() {
  clearTimeout(expiry);
  const panel = document.getElementById("agent-receipt");
  if (panel.open) panel.close();
  panel.querySelector("textarea").value = "";
}

export function showReceipt(args, receipt) {
  clearReceipt();
  const panel = document.getElementById("agent-receipt");
  const field = panel.querySelector("textarea");
  field.value = JSON.stringify({...args, interaction_receipt: receipt.interaction_receipt});
  panel.showModal();
  field.select();
  expiry = setTimeout(clearReceipt, Math.max(0, Date.parse(receipt.expires_at) - Date.now()));
}

const panel = document.getElementById("agent-receipt");
panel.querySelector("[data-copy-receipt]").addEventListener("click", async event => {
  try {
    await navigator.clipboard.writeText(panel.querySelector("textarea").value);
    event.currentTarget?.focus();
    panel.querySelector("[role=status]").textContent = "已复制调用参数。";
  } catch {
    panel.querySelector("textarea").select();
    panel.querySelector("[role=status]").textContent = "请手动复制已选中的调用参数。";
  }
});
panel.addEventListener("close", () => {
  if (!panel.open) {
    clearReceipt();
    panel.querySelector("[role=status]").textContent = "";
  }
});
