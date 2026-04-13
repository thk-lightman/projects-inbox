/**
 * AnyFlip 이미지 다운로더
 *
 * 사용법:
 *   1. AnyFlip 뷰어 페이지를 연다 (예: https://online.anyflip.com/itmik/xxrk/mobile/)
 *   2. 브라우저 개발자 도구 콘솔을 연다 (F12 → Console 탭)
 *   3. 아래 코드를 전부 복사해서 콘솔에 붙여넣고 Enter
 *   4. 콘솔에 [OK] 로그가 올라가며, 완료되면 images.zip 파일이 자동 다운로드된다
 *
 * 주의:
 *   - CONFIG_URL과 BASE_URL을 대상 책에 맞게 수정해야 한다
 *   - config.js URL은 브라우저 개발자 도구 > Network 탭에서 "config.js"를 검색하면 찾을 수 있다
 */

(async function(){
    // ===== 설정 =====
    var CONFIG_URL = 'https://online.anyflip.com/itmik/xxrk/mobile/javascript/config.js?1773353799';
    var BASE_URL   = 'https://online.anyflip.com/itmik/xxrk/';
    // ================

    // 1) JSZip 라이브러리 로드
    var s = document.createElement('script');
    s.src = 'https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js';
    document.head.appendChild(s);
    await new Promise(function(r){ s.onload = r });

    // 2) config.js를 텍스트로 가져와서, 이스케이프된 슬래시(\/)를 정상 슬래시(/)로 치환
    var res = await fetch(CONFIG_URL);
    var text = await res.text();
    text = text.replace(/\\\//g, '/');

    // 3) 정규식으로 "files/large/해시.webp" 패턴을 모두 추출 (중복 제거)
    var re = /files\/large\/[a-f0-9]+\.webp/g;
    var found = [];
    var m;
    while (m = re.exec(text)) {
        if (found.indexOf(m[0]) === -1) found.push(m[0]);
    }

    console.log('Found ' + found.length + ' images');
    if (found.length === 0) { alert('No images found'); return; }

    // 4) 각 이미지를 fetch하여 ZIP에 추가
    var zip = new JSZip();
    var ok = 0;

    for (var i = 0; i < found.length; i++) {
        var seq = String(i + 1).padStart(3, '0');
        try {
            var r = await fetch(BASE_URL + found[i]);
            if (!r.ok) throw new Error('HTTP ' + r.status);
            var blob = await r.blob();
            zip.file('image_' + seq + '.webp', blob);
            ok++;
            console.log('[OK] ' + seq + '/' + found.length + ' (' + blob.size + ' bytes)');
            await new Promise(function(r){ setTimeout(r, 300) });
        } catch(e) {
            console.error('[FAIL] ' + seq + ': ' + e.message);
        }
    }

    // 5) ZIP 생성 및 다운로드
    console.log('ZIP creating... success: ' + ok);
    var zb = await zip.generateAsync({ type: 'blob' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(zb);
    a.download = 'images.zip';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
    console.log('Done!');
})();
