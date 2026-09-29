chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    // 处理生成好的 ZIP 另存为
    if (request.action === "downloadZip") {
      chrome.downloads.download({
        url: request.url,
        filename: request.filename,
        saveAs: true // 全局只弹 1 次保存窗口
      }, (downloadId) => {
        sendResponse({ success: true, downloadId: downloadId });
      });
      return true;
    }
  
    // 跨域代理抓取文件/页面
    if (request.action === "fetchFile") {
      fetchFileAsBase64(request.url)
        .then(base64Data => {
          sendResponse({ success: true, data: base64Data });
        })
        .catch(err => {
          sendResponse({ success: false, error: err.message || "Fetch failed" });
        });
      return true; // 保持异步通信通道
    }
  });
  
  async function fetchFileAsBase64(url) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 15000); // 15 秒超时
  
    try {
      // 关键修正：必须带上 credentials: 'include' 才能通过 AO3 的验证
      const response = await fetch(url, {
        method: 'GET',
        credentials: 'include',
        headers: {
          'Accept': '*/*'
        },
        signal: controller.signal
      });
      
      clearTimeout(timeoutId);
  
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
  
      const blob = await response.blob();
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onloadend = () => resolve(reader.result);
        reader.onerror = () => reject(new Error("Blob to Base64 failed"));
        reader.readAsDataURL(blob);
      });
    } catch (error) {
      clearTimeout(timeoutId);
      throw error;
    }
  }