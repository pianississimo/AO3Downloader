let isTaskRunning = false;
let globalZip = null;
let globalFailedList = [];
let globalTotalSuccess = 0;
let globalFormat = "";
let retryCount = 0;

function initZipDownloaderUI() {
  const workList = document.querySelector("ol.work.index, ol.bookmark.index");
  if (!workList) return;

  if (document.getElementById("ao3-zip-panel")) return;

  const panel = document.createElement("div");
  panel.id = "ao3-zip-panel";
  panel.style.cssText = `
    background: #f4f6f8;
    border: 2px solid #2b579a;
    border-radius: 6px;
    padding: 12px 16px;
    margin: 15px 0;
    box-shadow: 0 2px 5px rgba(0,0,0,0.1);
  `;

  panel.innerHTML = `
    <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap;">
      <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
        <strong style="color: #2b579a; font-size: 14px;">📦 AO3 批量打包下载器</strong>
        <label style="font-size: 13px;">格式: 
          <select id="zip-format-select" style="padding: 3px 6px; font-size: 13px;">
            <option value="epub">EPUB</option>
            <option value="pdf">PDF</option>
            <option value="mobi">MOBI</option>
            <option value="azw3">AZW3</option>
            <option value="html">HTML</option>
          </select>
        </label>
        <label style="font-size: 13px;">页码范围: 
          第 <input type="number" id="zip-start-page" value="1" min="1" max="999" style="width: 45px; padding: 2px;"> 页 
          至 <input type="number" id="zip-end-page" value="3" min="1" max="999" style="width: 45px; padding: 2px;"> 页
        </label>
        <label style="font-size: 13px; font-weight: bold; color: #d32f2f;">间隔: 
          <input type="number" id="zip-delay-sec" value="4.0" min="1" max="30" step="0.5" style="width: 50px; padding: 2px;"> 秒
        </label>
      </div>
      <div>
        <button id="start-zip-btn" style="
          background: #2b579a; color: white; border: none; padding: 6px 16px; 
          font-weight: bold; border-radius: 4px; cursor: pointer; font-size: 13px;
        ">开始下载</button>
        <button id="stop-zip-btn" style="
          background: #666; color: white; border: none; padding: 6px 12px; 
          font-weight: bold; border-radius: 4px; cursor: pointer; font-size: 13px; display: none; margin-left: 8px;
        ">终止任务</button>
      </div>
    </div>
    
    <div style="font-size: 11px; color: #d32f2f; margin-top: 6px; line-height: 1.4;">
      ⚠️ <strong>风险提示：</strong>请求间隔过短易触发 AO3 频控机制导致批量失败。若遇大面积失败，建议暂停任务并冷却 3 至 5 分钟后再行重试。<br>
      ⚠️ <strong>Warning:</strong> Short request intervals may trigger AO3 rate limits. In case of bulk failures, it is recommended to pause, wait 3-5 minutes, and retry.
    </div>

    <div id="zip-status-display" style="font-size: 12px; color: #2b579a; margin-top: 8px; font-weight: bold;"></div>
    <div id="zip-failed-report" style="display: none; margin-top: 10px; padding-top: 10px; border-top: 1px dashed #ccc;"></div>
  `;

  workList.parentNode.insertBefore(panel, workList);

  document.getElementById("start-zip-btn").onclick = () => startZipProcess();
  document.getElementById("stop-zip-btn").onclick = () => {
    isTaskRunning = false;
    document.getElementById("zip-status-display").textContent = "正在终止任务...";
  };
}

async function startZipProcess() {
  globalFormat = document.getElementById("zip-format-select").value;
  const startPage = parseInt(document.getElementById("zip-start-page").value) || 1;
  const endPage = parseInt(document.getElementById("zip-end-page").value) || 1;
  const userDelaySec = parseFloat(document.getElementById("zip-delay-sec").value) || 4.0;
  const delayMs = userDelaySec * 1000;
  
  if (startPage > endPage) {
    alert("起始页不能大于结束页！");
    return;
  }

  const statusDiv = document.getElementById("zip-status-display");
  document.getElementById("zip-failed-report").style.display = "none";
  
  isTaskRunning = true;
  document.getElementById("start-zip-btn").style.display = "none";
  document.getElementById("stop-zip-btn").style.display = "inline-block";

  globalZip = new JSZip();
  globalFailedList = [];
  globalTotalSuccess = 0;
  retryCount = 0;
  
  let currentDoc = document;
  let currentPage = 1;

  // 如果起始页大于 1，我们需要先通过翻页链接导航到目标起始页
  if (startPage > 1) {
    statusDiv.textContent = `正在定位至起始页 (第 ${startPage} 页)...`;
    
    // 从当前页面开始尝试寻找目标页面的链接或逐页翻页
    while (currentPage < startPage && isTaskRunning) {
      const nextLink = currentDoc.querySelector("ol.pagination li.next a");
      if (!nextLink || !nextLink.href) {
        statusDiv.textContent = `定位失败：找不到通往第 ${startPage} 页的链接。`;
        isTaskRunning = false;
        break;
      }

      await new Promise(r => setTimeout(r, 1500));
      const pageFetchRes = await new Promise((resolve) => {
        chrome.runtime.sendMessage({ action: "fetchFile", url: nextLink.href }, resolve);
      });

      if (pageFetchRes && pageFetchRes.success) {
        try {
          const base64Str = pageFetchRes.data.split(',')[1];
          const binaryStr = atob(base64Str);
          const bytes = new Uint8Array(binaryStr.length);
          for (let k = 0; k < binaryStr.length; k++) bytes[k] = binaryStr.charCodeAt(k);
          const decodedHtml = new TextDecoder("utf-8").decode(bytes);

          const parser = new DOMParser();
          currentDoc = parser.parseFromString(decodedHtml, "text/html");
          currentPage++;
        } catch (err) {
          statusDiv.textContent = `跳转至第 ${startPage} 页时解析失败。`;
          isTaskRunning = false;
          break;
        }
      } else {
        statusDiv.textContent = `跳转至第 ${startPage} 页时请求失败。`;
        isTaskRunning = false;
        break;
      }
    }
  }

  // 开始在指定的 [startPage, endPage] 范围内抓取
  while (currentPage <= endPage && isTaskRunning) {
    const works = currentDoc.querySelectorAll("li.work.blurb, li.bookmark.blurb");
    statusDiv.textContent = `正在索引第 ${currentPage}/${endPage} 页，共 ${works.length} 项...`;

    for (let i = 0; i < works.length; i++) {
      if (!isTaskRunning) break;

      const work = works[i];
      const workId = work.id.replace("work_", "").replace("bookmark_", "");
      if (!workId) continue;

      const titleEl = work.querySelector("h4.heading a");
      const workTitle = titleEl ? titleEl.textContent.trim() : `Work_${workId}`;
      const workUrl = `https://archiveofourown.org/works/${workId}`;
      const downloadUrl = `https://archiveofourown.org/downloads/${workId}/${encodeURIComponent(workTitle)}.${globalFormat}`;

      statusDiv.textContent = `[页 ${currentPage}/${endPage}] 正在抓取 (${i + 1}/${works.length}): ${workTitle} (成功: ${globalTotalSuccess}, 失败: ${globalFailedList.length})`;

      const fetchRes = await new Promise((resolve) => {
        chrome.runtime.sendMessage({ action: "fetchFile", url: downloadUrl }, resolve);
      });

      if (fetchRes && fetchRes.success) {
        try {
          const base64Content = fetchRes.data.split(',')[1];
          const cleanTitle = workTitle.replace(/[/\\?%*:|"<>]/g, "_");
          globalZip.file(`${cleanTitle}_[${workId}].${globalFormat}`, base64Content, { base64: true });
          globalTotalSuccess++;
        } catch (e) {
          globalFailedList.push({ title: workTitle, url: workUrl, downloadUrl, reason: "Encoding Error" });
        }
      } else {
        globalFailedList.push({ title: workTitle, url: workUrl, downloadUrl, reason: fetchRes ? fetchRes.error : "Unknown Error" });
      }

      await new Promise(r => setTimeout(r, delayMs));
    }

    if (currentPage >= endPage || !isTaskRunning) break;

    const nextLink = currentDoc.querySelector("ol.pagination li.next a");
    if (nextLink && nextLink.href) {
      statusDiv.textContent = `正在请求下一页 (第 ${currentPage + 1} 页)...`;
      await new Promise(r => setTimeout(r, 2000));
      
      const pageFetchRes = await new Promise((resolve) => {
        chrome.runtime.sendMessage({ action: "fetchFile", url: nextLink.href }, resolve);
      });

      if (pageFetchRes && pageFetchRes.success) {
        try {
          const base64Str = pageFetchRes.data.split(',')[1];
          const binaryStr = atob(base64Str);
          const bytes = new Uint8Array(binaryStr.length);
          for (let k = 0; k < binaryStr.length; k++) bytes[k] = binaryStr.charCodeAt(k);
          const decodedHtml = new TextDecoder("utf-8").decode(bytes);

          const parser = new DOMParser();
          currentDoc = parser.parseFromString(decodedHtml, "text/html");
          currentPage++;
        } catch (err) {
          statusDiv.textContent = `解析下一页 HTML 失败，任务终止。`;
          break;
        }
      } else {
        statusDiv.textContent = `获取下一页失败，任务终止。`;
        break;
      }
    } else {
      break;
    }
  }

  checkAndShowResults();
}

async function triggerZipDownloadAsync() {
  if (globalTotalSuccess === 0) return;
  
  const statusDiv = document.getElementById("zip-status-display");
  statusDiv.textContent = `正在生成压缩包 (${globalTotalSuccess} 项)...`;

  const zipBlob = await globalZip.generateAsync({ type: "blob" });
  const blobUrl = URL.createObjectURL(zipBlob);
  const timestamp = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  const retrySuffix = retryCount > 0 ? `_Retry${retryCount}` : "";
  
  chrome.runtime.sendMessage({
    action: "downloadZip",
    url: blobUrl,
    filename: `AO3_Export_${timestamp}${retrySuffix}_${globalTotalSuccess}items.${globalFormat}.zip`
  });
}

function checkAndShowResults() {
  const statusDiv = document.getElementById("zip-status-display");
  document.getElementById("start-zip-btn").style.display = "inline-block";
  document.getElementById("stop-zip-btn").style.display = "none";
  isTaskRunning = false;

  if (globalFailedList.length > 0) {
    statusDiv.textContent = `任务暂停：成功 ${globalTotalSuccess} 项，失败 ${globalFailedList.length} 项。请选择后续操作：`;
    renderFailedReport(true); 
  } else if (globalTotalSuccess > 0) {
    triggerZipDownloadAsync().then(() => {
      statusDiv.textContent = `任务完成：成功导出 ${globalTotalSuccess} 项。`;
    });
  } else {
    statusDiv.textContent = `任务结束：未获取到有效文件。`;
  }
}

function renderFailedReport(showRetryOptions) {
  const container = document.getElementById("zip-failed-report");
  container.style.display = "block";
  
  let actionsHtml = `<button id="copy-zip-failed-btn" style="padding: 4px 8px; font-size: 12px; cursor: pointer; background: #666; color: #fff; border: none; border-radius: 3px;">复制失败链接</button>`;

  if (showRetryOptions) {
    actionsHtml = `
      <button id="retry-failed-btn" style="padding: 4px 8px; font-size: 12px; cursor: pointer; background: #ff9800; color: #fff; border: none; border-radius: 3px; font-weight: bold; margin-right: 8px;">下载并重试</button>
      <button id="force-pack-btn" style="padding: 4px 8px; font-size: 12px; cursor: pointer; background: #2b579a; color: #fff; border: none; border-radius: 3px; font-weight: bold; margin-right: 8px;">仅下载成功项</button>
      ${actionsHtml}
    `;
  }

  container.innerHTML = `
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
      <strong style="color: #c00; font-size: 13px;">失败列表 (${globalFailedList.length} 项)：</strong>
      <div>${actionsHtml}</div>
    </div>
    <ul style="max-height: 180px; overflow-y: auto; background: #fff; border: 1px solid #ddd; padding: 8px 12px; margin: 0; font-size: 12px; list-style: decimal inside;">
      ${globalFailedList.map(item => `
        <li style="margin-bottom: 4px;">
          <a href="${item.url}" target="_blank" style="color: #900; text-decoration: underline;">${item.title}</a> 
          <span style="color: #888;">[错误: ${item.reason}]</span>
        </li>
      `).join('')}
    </ul>
  `;

  document.getElementById("copy-zip-failed-btn").onclick = () => {
    const textToCopy = globalFailedList.map(item => `${item.title}: ${item.downloadUrl}`).join("\n");
    navigator.clipboard.writeText(textToCopy).then(() => alert("已复制所有失败链接至剪贴板。"));
  };

  if (showRetryOptions) {
    document.getElementById("retry-failed-btn").onclick = () => doRetryFailed();
    document.getElementById("force-pack-btn").onclick = async () => {
      await triggerZipDownloadAsync();
      document.getElementById("zip-status-display").textContent = `任务完成：已导出成功项，跳过失败项。`;
      renderFailedReport(false); 
    };
  }
}

async function doRetryFailed() {
  const statusDiv = document.getElementById("zip-status-display");
  document.getElementById("start-zip-btn").style.display = "none";
  document.getElementById("stop-zip-btn").style.display = "inline-block";
  document.getElementById("zip-failed-report").style.display = "none";

  isTaskRunning = true;

  if (globalTotalSuccess > 0) {
    await triggerZipDownloadAsync();
    statusDiv.textContent = `第一批文件已导出。正在准备重试队列...`;
    await new Promise(r => setTimeout(r, 2000));
  }

  retryCount++;
  const itemsToRetry = [...globalFailedList];
  globalFailedList = []; 
  globalZip = new JSZip(); 
  globalTotalSuccess = 0;  

  const userDelaySec = parseFloat(document.getElementById("zip-delay-sec").value) || 4.0;
  const retryDelayMs = (userDelaySec + 2.0) * 1000;

  statusDiv.textContent = `正在进入防风控冷却期 (5秒)...`;
  await new Promise(r => setTimeout(r, 5000));

  for (let i = 0; i < itemsToRetry.length; i++) {
    if (!isTaskRunning) break;

    const item = itemsToRetry[i];
    statusDiv.textContent = `[重试 ${retryCount}] (${i+1}/${itemsToRetry.length}) : ${item.title} (新增成功: ${globalTotalSuccess})`;

    const fetchRes = await new Promise((resolve) => {
      chrome.runtime.sendMessage({ action: "fetchFile", url: item.downloadUrl }, resolve);
    });

    if (fetchRes && fetchRes.success) {
      try {
        const base64Content = fetchRes.data.split(',')[1];
        const cleanTitle = item.title.replace(/[/\\?%*:|"<>]/g, "_");
        const workId = item.url.split("/works/")[1] || "unknown";
        globalZip.file(`${cleanTitle}_[${workId}].${globalFormat}`, base64Content, { base64: true });
        globalTotalSuccess++;
      } catch (e) {
        item.reason = "Encoding Error";
        globalFailedList.push(item);
      }
    } else {
      item.reason = fetchRes ? fetchRes.error : "Unknown Error";
      globalFailedList.push(item);
    }

    await new Promise(r => setTimeout(r, retryDelayMs));
  }

  checkAndShowResults();
}

initZipDownloaderUI();