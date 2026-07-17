// Notifications and polling for unread inquiries (mdb-engine auto-CRUD backed)
(function () {
    'use strict';

    let lastUnreadCount = 0;
    let pollingInterval = null;
    const POLL_INTERVAL = 30000; // 30 seconds

    // Store base path ("" on platform pages, "/{store}" on a store's pages).
    // API paths stay relative here and are prefixed at fetch time (apiJson).
    const API_INQUIRIES = '/api/inquiries';
    const API_UNREAD_COUNT = '/api/inquiries/_count?scope=unread';

    // True when the current page is an admin page for the active store, i.e.
    // "/admin/..." or "/{store}/admin/...". base_path is stripped first.
    function isAdminPath() {
        const bp = window.BASE_PATH || '';
        let p = window.location.pathname;
        if (bp && p.startsWith(bp)) p = p.slice(bp.length);
        return p.startsWith('/admin/');
    }

    function generateAvatarUrl(identifier) {
        if (!identifier) return 'https://api.dicebear.com/8.x/rings/svg?seed=default';
        const seed = encodeURIComponent(identifier);
        return `https://api.dicebear.com/8.x/rings/svg?seed=${seed}`;
    }

    async function apiJson(url, opts = {}) {
        const res = await fetch((window.BASE_PATH || '') + url, {
            credentials: 'same-origin',
            ...opts,
            headers: {
                Accept: 'application/json',
                ...(opts.headers || {}),
                ...(opts.body ? { 'Content-Type': 'application/json' } : {})
            }
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || err.error || `HTTP ${res.status}`);
        }
        return res.json();
    }

    async function initializeNotifications() {
        if (!('serviceWorker' in navigator) || !('Notification' in window)) return false;
        try {
            await navigator.serviceWorker.register('/static/service-worker.js');
            await navigator.serviceWorker.ready;
            if (Notification.permission === 'default') {
                const permission = await Notification.requestPermission();
                if (permission !== 'granted') return false;
            } else if (Notification.permission === 'denied') {
                return false;
            }
            return true;
        } catch (error) {
            console.error('Error initializing notifications:', error);
            return false;
        }
    }

    function showNotification(title, body, data = {}) {
        if (Notification.permission !== 'granted') return;
        const opts = {
            body,
            icon: '/static/icons/icon-192x192.png',
            badge: '/static/icons/icon-192x192.png',
            tag: 'new-inquiry',
            data,
            vibrate: [200, 100, 200]
        };
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.ready.then((reg) => reg.showNotification(title, opts));
        } else {
            new Notification(title, opts);
        }
    }

    async function checkUnreadInquiries() {
        try {
            const data = await apiJson(API_UNREAD_COUNT);
            const current = data.count ?? data.total ?? 0;
            if (current > lastUnreadCount && lastUnreadCount > 0) {
                const diff = current - lastUnreadCount;
                showNotification('New Inquiry', diff === 1 ? 'You have a new unread inquiry' : `You have ${diff} new unread inquiries`, {
                    url: (window.BASE_PATH || '') + '/admin/inquiries'
                });
            }
            lastUnreadCount = current;
            await updateUnreadBadge(current);
            return current;
        } catch (error) {
            console.error('Error checking unread inquiries:', error);
            return lastUnreadCount;
        }
    }

    async function updateUnreadBadge(count) {
        const dashboardBadge = document.getElementById('unread-inquiries-badge');
        const inquiriesBadge = document.getElementById('unread-count-badge');
        [dashboardBadge, inquiriesBadge].forEach((el) => {
            if (!el) return;
            if (count > 0) {
                el.textContent = count > 99 ? '99+' : count;
                el.classList.remove('hidden');
            } else {
                el.classList.add('hidden');
            }
        });
        document.title = count > 0
            ? `(${count}) ${document.title.replace(/^\(\d+\)\s*/, '')}`
            : document.title.replace(/^\(\d+\)\s*/, '');
        try {
            if ('serviceWorker' in navigator) {
                const reg = await navigator.serviceWorker.ready;
                if ('setAppBadge' in reg) {
                    if (count > 0) await reg.setAppBadge(count > 99 ? 99 : count);
                    else await reg.clearAppBadge();
                    return;
                }
            }
            if ('setAppBadge' in navigator) {
                if (count > 0) await navigator.setAppBadge(count > 99 ? 99 : count);
                else await navigator.clearAppBadge();
            }
        } catch (error) {
            console.error('Badge update failed:', error);
        }
    }

    function startPolling() {
        if (pollingInterval) clearInterval(pollingInterval);
        checkUnreadInquiries();
        pollingInterval = setInterval(checkUnreadInquiries, POLL_INTERVAL);
    }

    function stopPolling() {
        if (pollingInterval) {
            clearInterval(pollingInterval);
            pollingInterval = null;
        }
    }

    async function markInquiryRead(inquiryId) {
        try {
            await apiJson(`${API_INQUIRIES}/${inquiryId}`, {
                method: 'PATCH',
                body: JSON.stringify({ read: true })
            });
            const el = document.querySelector(`[data-inquiry-id="${inquiryId}"]`);
            if (el) {
                el.classList.remove('unread');
                el.classList.add('read');
            }
            await checkUnreadInquiries();
            return { success: true };
        } catch (error) {
            console.error('Error marking inquiry as read:', error);
            return { success: false, error: error.message };
        }
    }

    async function markAllInquiriesRead() {
        try {
            const list = await apiJson(`${API_INQUIRIES}?scope=unread&limit=500`);
            const ids = (list.data || []).map((doc) => doc._id?.$oid || doc._id).filter(Boolean);
            await Promise.all(ids.map((id) => apiJson(`${API_INQUIRIES}/${id}`, {
                method: 'PATCH',
                body: JSON.stringify({ read: true })
            })));
            document.querySelectorAll('.unread').forEach((el) => {
                el.classList.remove('unread');
                el.classList.add('read');
            });
            await checkUnreadInquiries();
            return { success: true };
        } catch (error) {
            console.error('Error marking all inquiries as read:', error);
            return { success: false, error: error.message };
        }
    }

    document.addEventListener('DOMContentLoaded', async () => {
        if (!isAdminPath()) return;
        await initializeNotifications();
        if ('serviceWorker' in navigator) {
            try { await navigator.serviceWorker.ready; } catch (_) {}
        }
        const unread = await checkUnreadInquiries();
        setTimeout(() => updateUnreadBadge(unread), 1000);
        startPolling();
        window.inquiryNotifications = {
            checkUnreadInquiries,
            markInquiryRead,
            markAllInquiriesRead,
            generateAvatarUrl,
            updateUnreadBadge
        };
    });

    document.addEventListener('visibilitychange', () => {
        if (document.hidden) stopPolling();
        else if (isAdminPath()) startPolling();
    });

    window.addEventListener('beforeunload', stopPolling);

    window.InquiryNotifications = {
        checkUnreadInquiries,
        markInquiryRead,
        markAllInquiriesRead,
        generateAvatarUrl,
        updateUnreadBadge,
        startPolling,
        stopPolling
    };
})();
