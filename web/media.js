"use strict";

// Layout offsets are Unicode code points, matching Python, rather than UTF-16 code units.
let composerMedia = [], mediaBody = "", mediaUploading = false;
const mediaAccept = ".jpg,.jpeg,.png,.gif,.webp,.mp4,.webm,.mov";
function codepointOffset(text, utf16) { return Array.from(text.slice(0, utf16)).length; }
function shiftedMedia(items, before, after) {
  const old = Array.from(before), next = Array.from(after);
  let first = 0, tail = 0;
  while (first < old.length && first < next.length && old[first] === next[first]) first++;
  while (tail < old.length - first && tail < next.length - first && old[old.length - 1 - tail] === next[next.length - 1 - tail]) tail++;
  const end = old.length - tail, delta = next.length - old.length;
  return items.map(a => ({ ...a, position: Math.max(0, Math.min(next.length,
    a.position <= first ? a.position : a.position >= end ? a.position + delta : first)) }));
}
function mediaURL(id) { return /^[a-f0-9]{32}$/.test(id || "") ? `/api/media/${id}` : ""; }
function mediaDisplayName(a, day) {
  const original = a.name || "附件";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day || "")) return original;
  const parsed = new Date(`${day}T12:00:00Z`);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0,10) !== day) return original;
  const name = original.replace(/[<>:"/\\|?*\x00-\x1f\x7f]/g, "_").replace(/[. ]+$/g, "");
  const dot = name.lastIndexOf(".");
  let stem = Array.from((dot > 0 ? name.slice(0,dot) : name).replace(/[. ]+$/g, "")).slice(0,110).join("") || "附件";
  if (/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/i.test(stem.split(".")[0])) stem = "_" + stem;
  const extension = a.extension || (dot > 0 ? name.slice(dot).toLowerCase() : "");
  const suffix = "_" + day;
  return stem + (stem.endsWith(suffix) ? "" : suffix) + extension;
}
function mediaDownloadURL(a) {
  const params = new URLSearchParams({download:"1"});
  if (a.entry_id && a.media_revision) { params.set("entry",a.entry_id); params.set("revision",a.media_revision); }
  return mediaURL(a.id) + "?" + params;
}
function mediaFigure(a, preview = false) {
  const url = a.localURL || mediaURL(a.id);
  if (!url) return "";
  const name = escapeHTML(mediaDisplayName(a, preview ? $("#entry-day").value : a.journal_day)), src = escapeHTML(url);
  return `<figure class="journal-media">${(a.mime || "").startsWith("image/")
    ? `<button type="button" class="photo-open" data-photo="${src}" data-photo-name="${name}" aria-label="放大查看 ${name}"><img src="${src}" alt="${name}" loading="lazy" decoding="async"></button>`
    : `<video src="${src}" controls playsinline preload="metadata" aria-label="${name}"></video>`}
    ${!preview && a.id ? `<figcaption title="原始文件名：${escapeHTML(a.name)}">${name}<a href="${escapeHTML(mediaDownloadURL(a))}" download>下载原件 ↗</a></figcaption>` : ""}</figure>`;
}
function mixedMediaHTML(body, attachments = [], preview = false) {
  const points = Array.from(body || ""), items = attachments.filter(a => a.id || a.localURL)
    .map((a, index) => ({ ...a, index })).sort((a,b) => a.position - b.position || a.index - b.index);
  let offset = 0, html = "";
  for (const a of items) {
    const position = Math.max(offset, Math.min(points.length, a.position));
    if (position > offset) html += `<p class="entry-body">${escapeHTML(points.slice(offset, position).join(""))}</p>`;
    html += mediaFigure(a, preview); offset = position;
  }
  if (offset < points.length) html += `<p class="entry-body">${escapeHTML(points.slice(offset).join(""))}</p>`;
  return `<div class="journal-content">${html}</div>`;
}
function mediaDraft() { return composerMedia.filter(a => a.id).map(({localURL, file, error, display_name, download_url, journal_day, entry_id, media_revision, ...a}) => a); }
function mediaLayout() { return composerMedia.filter(a => a.id).map(a => ({ id:a.id, position:a.position })); }
function mediaHasDraft() { return composerMedia.length > 0; }
function mediaBusy() {
  if (!mediaUploading && !state.saving) return false;
  toast(mediaUploading ? "附件正在上传，请稍候" : "正在保存，请稍候"); return true;
}
function setComposerMedia(items = []) {
  for (const a of composerMedia) {
    if (a.localURL) URL.revokeObjectURL(a.localURL);
    if (a.id && !a.published && !items.some(item => item.id === a.id) && state.token)
      api("/api/media/discard", {id:a.id}).catch(() => {});
  }
  composerMedia = items.map(a => ({...a}));
  mediaBody = $("#entry-body").value;
  renderComposerMedia();
}
function renderComposerMedia() {
  const strip = $("#media-items"), preview = $("#media-preview");
  if (!strip) return;
  composerMedia.sort((a,b) => a.position - b.position);
  $("#media-tools").hidden = !composerMedia.length;
  strip.innerHTML = composerMedia.map((a,i) => `<div class="media-item ${a.error ? "media-failed" : ""}">
    ${(a.mime || "").startsWith("image/") ? `<img src="${escapeHTML(a.localURL || mediaURL(a.id))}" alt="" loading="lazy">` : '<span class="media-video-mark" aria-hidden="true">▷</span>'}
    <div class="media-item-info"><strong title="原始文件名：${escapeHTML(a.name)}">${escapeHTML(mediaDisplayName(a, $("#entry-day").value))}</strong><small>${a.error ? escapeHTML(a.error) : a.id ? `插在第 ${a.position} 字后 · ${(a.size / 1024 / 1024).toFixed(1)} MB` : "正在上传到本机…"}</small></div>
    <div class="media-item-actions"><button type="button" data-media-move="${i}" class="text-button" title="移到当前文字光标位置">移到光标</button><button type="button" data-media-up="${i}" class="icon-button" aria-label="前移 ${escapeHTML(a.name)}" ${i === 0 ? "disabled" : ""}>↑</button><button type="button" data-media-remove="${i}" class="icon-button" aria-label="移除 ${escapeHTML(a.name)}">×</button></div></div>`).join("");
  if (preview.open) $("#media-preview-content").innerHTML = mixedMediaHTML($("#entry-body").value, composerMedia, true);
  $("#save-entry").disabled = state.saving || mediaUploading;
}
function uploadBinary(path, file, progress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path); xhr.setRequestHeader("X-Notes-Token", state.token);
    xhr.setRequestHeader("Content-Type", "application/octet-stream"); xhr.timeout = 600000;
    xhr.upload.onprogress = e => { if (e.lengthComputable) progress?.(Math.round(e.loaded / e.total * 100)); };
    xhr.onload = () => {
      let result; try { result = JSON.parse(xhr.responseText); } catch { reject(new Error("本地服务未能完成上传")); return; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(result);
      else reject(new Error(result.error || "上传失败，请重试"));
    };
    xhr.onerror = () => reject(new Error("上传中断，文字草稿已保留，请重新选择附件"));
    xhr.ontimeout = () => reject(new Error("上传超时，请重新选择附件"));
    xhr.send(file);
  });
}
async function addMedia(files) {
  if (mediaBusy()) return;
  if (!state.token) { toast("请等待本地服务连接后添加附件"); return; }
  const picked = Array.from(files);
  if (!picked.length) return;
  if (composerMedia.length + picked.length > 60) { toast("每条日记最多添加 60 个附件"); return; }
  const body = $("#entry-body");
  const position = codepointOffset(body.value, body.selectionStart);
  mediaUploading = true;
  const pending = picked.map(file => ({file, name:file.name || "粘贴的图片.png", mime:file.type, position, localURL:URL.createObjectURL(file)}));
  composerMedia.push(...pending); renderComposerMedia();
  try {
    for (const item of pending) {
      try {
        const image = item.mime.startsWith("image/");
        if (item.file.size > (image ? 25 : 512) * 1024 * 1024) throw new Error(image ? "图片最大 25 MB" : "视频最大 512 MB");
        const result = await uploadBinary(`/api/media/upload?name=${encodeURIComponent(item.name)}`, item.file, percent => {
          const index = composerMedia.indexOf(item), detail = $$(".media-item-info small", $("#media-items"))[index];
          if (detail) detail.textContent = `正在上传到本机 · ${percent}%`;
        });
        Object.assign(item, result.attachment); delete item.file;
      } catch(error) { item.error = error.message; }
      renderComposerMedia(); draftWrite();
    }
  } finally { mediaUploading = false; renderComposerMedia(); draftWrite(); }
  if (pending.some(a => a.error)) toast("部分附件未能上传，请移除后重新添加；文字草稿已保留");
  else toast("附件已上传，保存日记后就会留下来");
}
function initMedia() {
  const input = $("#media-files"), body = $("#entry-body"), composer = $("#composer");
  input.accept = mediaAccept;
  $("#add-media").onclick = () => { if (!mediaBusy()) input.click(); };
  input.onchange = () => { addMedia(input.files); input.value = ""; };
  $("#entry-day").addEventListener("change", renderComposerMedia);
  mediaBody = body.value;
  body.addEventListener("input", () => {
    const shifted = shiftedMedia(composerMedia, mediaBody, body.value);
    composerMedia.forEach((item, i) => { item.position = shifted[i].position; }); mediaBody = body.value;
    renderComposerMedia(); draftWrite();
  });
  body.addEventListener("paste", event => {
    const files = Array.from(event.clipboardData?.files || []);
    if (files.length) { event.preventDefault(); addMedia(files); }
  });
  composer.addEventListener("dragover", event => {
    if (Array.from(event.dataTransfer.types).includes("Files")) { event.preventDefault(); composer.classList.add("media-dragover"); }
  });
  composer.addEventListener("dragleave", event => { if (!composer.contains(event.relatedTarget)) composer.classList.remove("media-dragover"); });
  composer.addEventListener("drop", event => {
    composer.classList.remove("media-dragover");
    if (event.dataTransfer.files.length) { event.preventDefault(); addMedia(event.dataTransfer.files); }
  });
  $("#media-items").addEventListener("click", event => {
    const button = event.target.closest("button"); if (!button || mediaBusy()) return;
    const d = button.dataset;
    if (d.mediaRemove !== undefined) {
      const [item] = composerMedia.splice(Number(d.mediaRemove), 1);
      if (item.localURL) URL.revokeObjectURL(item.localURL);
      if (item.id && !item.published) api("/api/media/discard", { id:item.id }).catch(() => {});
    } else if (d.mediaMove !== undefined) composerMedia[Number(d.mediaMove)].position = codepointOffset(body.value, body.selectionStart);
    else if (d.mediaUp !== undefined) {
      const index = Number(d.mediaUp), previous = composerMedia[index - 1];
      if (previous) { composerMedia[index].position = previous.position; [composerMedia[index-1],composerMedia[index]] = [composerMedia[index], previous]; }
    }
    renderComposerMedia(); draftWrite();
  });
  $("#media-preview").addEventListener("toggle", renderComposerMedia);
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-photo]");
    if (!button) return;
    const url = button.dataset.photo;
    if (!url.startsWith("/api/media/") && !url.startsWith("blob:")) return;
    showModal(`<div class="photo-viewer"><img src="${escapeHTML(url)}" alt="${escapeHTML(button.dataset.photoName)}"></div><div class="modal-actions"><button class="secondary-button" data-close-modal>关闭</button></div>`);
  });
  document.addEventListener("error", event => {
    if (event.target.tagName === "VIDEO") event.target.parentElement.classList.add("media-unplayable");
  }, true);
  renderComposerMedia();
}
