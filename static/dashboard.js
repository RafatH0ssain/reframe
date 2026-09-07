            let photos = [];
            let pagination = {};
            const requestedInitialPage = parseInt(new URLSearchParams(window.location.search).get('page'), 10);
            let currentPage = Number.isInteger(requestedInitialPage) && requestedInitialPage > 0 ? requestedInitialPage : 1;
            let latestPhotoLoadRequest = 0;
            let extensionActions = [];
            let arenaTokenShouldClear = false;
            let settingsFormSnapshot = null;
            let settingsPageScrollY = 0;
            const photosPerPage = 12;  // Show 12 photos per page
            
            // Function to notify backend of user activity
            async function notifyUserActivity() {
                try {
                    await fetch('/api/timeout/reset', { method: 'POST' });
                } catch (error) {
                    console.log('Could not notify user activity:', error);
                }
            }
            
            async function loadPhotos(page = currentPage) {
                const requestedPage = Math.max(1, parseInt(page, 10) || 1);
                const requestId = ++latestPhotoLoadRequest;
                try {
                    await loadExtensionActions();
                    const response = await fetch(`/api/photos?page=${requestedPage}&limit=${photosPerPage}`);
                    if (!response.ok) {
                        // Server returned an error — skip this poll, will retry
                        console.log('Photos API returned', response.status, '— will retry');
                        return;
                    }
                    const data = await response.json();

                    // Ignore older refreshes that finished after a newer page request.
                    if (requestId !== latestPhotoLoadRequest) {
                        return;
                    }

                    const nextPagination = data.pagination || {};
                    const totalPages = Math.max(1, nextPagination.total_pages || 1);
                    if (requestedPage > totalPages) {
                        loadPhotos(totalPages);
                        return;
                    }

                    photos = data.photos || [];
                    pagination = nextPagination;
                    currentPage = requestedPage;
                    updatePageUrl();

                    renderGallery();
                    updatePhotoCount();
                    renderPagination();
                } catch (error) {
                    console.error('Error loading photos:', error);
                }
            }

            async function loadExtensionActions() {
                try {
                    const response = await fetch('/api/extensions/actions');
                    if (!response.ok) {
                        extensionActions = [];
                        return;
                    }
                    const data = await response.json();
                    extensionActions = data.actions || [];
                } catch (error) {
                    console.error('Error loading extension actions:', error);
                    extensionActions = [];
                }
            }
            
            async function clearScreen() {
                try {
                    const btn = document.querySelector('button[onclick="clearScreen()"]');
                    if (btn) btn.disabled = true;
                    const resp = await fetch('/api/display/clear', { method: 'POST' });
                    if (!resp.ok) throw new Error(await resp.text());
                    const data = await resp.json();
                    alert(data.message || 'screen cleared');
                } catch (error) {
                    console.error('Error clearing screen:', error);
                    alert('failed to clear screen');
                } finally {
                    const btn = document.querySelector('button[onclick="clearScreen()"]');
                    if (btn) btn.disabled = false;
                }
            }

            function renderGallery() {
                const grid = document.getElementById('photo-grid');
                
                if (photos.length === 0) {
                    grid.innerHTML = '<div class="loading">no photos found. capture your first photo!</div>';
                    return;
                }
                
                grid.innerHTML = photos.map(photo => `
                    <div class="photo-card" data-photo-id="${photo.id}">
                        <img 
                            src="${photo.dithered_path || photo.original_path}" 
                            alt="Photo ${photo.id}"
                            class="photo-image"
                        />
                        ${renderProvenance(photo)}
                        <div class="photo-info">
                            <div class="photo-actions">
                                <a href="${photo.original_path}" class="action-btn btn-primary" download onclick="event.stopPropagation()">
                                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="none" viewBox="0 0 48 48" style="margin-right: 5px;">
                                        <path fill="currentColor" d="M26 6a2 2 0 1 0-4 0h4Zm-3.414 37.414a2 2 0 0 0 2.828 0l12.728-12.728a2 2 0 1 0-2.828-2.828L24 39.172 12.686 27.858a2 2 0 1 0-2.828 2.828l12.728 12.728ZM24 6h-2v36h4V6h-2Z"/>
                                    </svg>
                                    original
                                </a>
                                ${photo.has_dithered ? `
                                    <a href="/api/download/dithered/${encodeURIComponent(photo.dithered_path.split('/').pop())}" class="action-btn btn-secondary" download onclick="event.stopPropagation()">
                                        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="none" viewBox="0 0 48 48" style="margin-right: 5px;">
                                            <path fill="currentColor" d="M10 28h4v4h-4v-4Zm4 4h4v4h-4v-4Z"/>
                                            <path fill="currentColor" d="M14 32h4v4h-4v-4Zm4 4h4v4h-4v-4Zm20-8h-4v4h4v-4Zm-4 4h-4v4h4v-4Zm-4 4h-4v4h4v-4Zm-8 4h4v4h-4v-4Zm0-4h4v4h-4v-4Zm0-4h4v4h-4v-4Zm0-4h4v4h-4v-4Zm0-4h4v4h-4v-4Zm0-4h4v4h-4v-4Zm0-4h4v4h-4v-4Zm0-4h4v4h-4v-4Zm0-4h4v4h-4V8Zm0-4h4v4h-4V4Z"/>
                                        </svg>
                                        dithered
                                    </a>
                                ` : ''}
                                <button class="action-btn btn-success" onclick="event.stopPropagation(); displayPhoto('${photo.id}')">
                                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="none" viewBox="0 0 48 48" style="margin-right: 5px;">
                                        <path fill="currentColor" d="M6 30h12v12H6V30Zm12-12h12v12H18V18ZM30 6h12v12H30V6Zm0 24h12v12H30V30ZM6 6h12v12H6V6Z"/>
                                    </svg>
                                    display
                                </button>
                                ${renderExtensionButtons(photo)}
                            </div>
                        </div>
                    </div>
                `).join('');
                
                // Add click handlers for photo cards
                setupPhotoCardHandlers();
            }

            function renderExtensionButtons(photo) {
                return extensionActions
                    .filter(action => !action.requires_dithered || photo.has_dithered)
                    .map(action => `
                        <button class="action-btn btn-secondary" data-extension-id="${action.id}" data-photo-id="${photo.id}" onclick="event.stopPropagation(); runExtensionAction('${action.id}', '${photo.id}', this)">
                            ${action.id === 'arena' ? `
                                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="none" viewBox="0 0 48 48" style="margin-right: 5px; transform: rotate(180deg);">
                                    <path fill="currentColor" d="M26 6a2 2 0 1 0-4 0h4Zm-3.414 37.414a2 2 0 0 0 2.828 0l12.728-12.728a2 2 0 1 0-2.828-2.828L24 39.172 12.686 27.858a2 2 0 1 0-2.828 2.828l12.728 12.728ZM24 6h-2v36h4V6h-2Z"/>
                                </svg>
                            ` : ''}
                            ${action.action_label}
                        </button>
                    `).join('');
            }
            
            function updatePhotoCount() {
                if (pagination.total_photos !== undefined) {
                    document.getElementById('photo-count').textContent = `${pagination.total_photos} photos`;
                } else {
                    document.getElementById('photo-count').textContent = `${photos.length} photos`;
                }
            }
            
            function renderPagination() {
                const paginationDiv = document.getElementById('pagination');
                const pageNumbersDiv = document.getElementById('page-numbers');
                const paginationInfo = document.getElementById('pagination-info');
                const prevBtn = document.getElementById('prev-btn');
                const nextBtn = document.getElementById('next-btn');
                const pageJumpInput = document.getElementById('page-jump-input');
                
                if (!pagination || pagination.total_pages <= 1) {
                    paginationDiv.style.display = 'none';
                    return;
                }
                
                paginationDiv.style.display = 'flex';
                
                // Update navigation buttons
                prevBtn.disabled = !pagination.has_prev;
                nextBtn.disabled = !pagination.has_next;
                pageJumpInput.max = pagination.total_pages;
                pageJumpInput.value = currentPage;
                
                // Update pagination info
                const startItem = ((currentPage - 1) * photosPerPage) + 1;
                const endItem = Math.min(currentPage * photosPerPage, pagination.total_photos);
                paginationInfo.textContent = `${startItem}-${endItem} of ${pagination.total_photos}`;
                
                // Generate page numbers
                pageNumbersDiv.innerHTML = '';
                const maxVisiblePages = 5;
                let startPage = Math.max(1, currentPage - Math.floor(maxVisiblePages / 2));
                let endPage = Math.min(pagination.total_pages, startPage + maxVisiblePages - 1);
                
                // Adjust start page if we're near the end
                if (endPage - startPage + 1 < maxVisiblePages) {
                    startPage = Math.max(1, endPage - maxVisiblePages + 1);
                }
                
                // Add first page and ellipsis if needed
                if (startPage > 1) {
                    addPageButton(1);
                    if (startPage > 2) {
                        const ellipsis = document.createElement('span');
                        ellipsis.textContent = '...';
                        ellipsis.className = 'pagination-ellipsis';
                        pageNumbersDiv.appendChild(ellipsis);
                    }
                }
                
                // Add visible page numbers
                for (let i = startPage; i <= endPage; i++) {
                    addPageButton(i);
                }
                
                // Add last page and ellipsis if needed
                if (endPage < pagination.total_pages) {
                    if (endPage < pagination.total_pages - 1) {
                        const ellipsis = document.createElement('span');
                        ellipsis.textContent = '...';
                        ellipsis.className = 'pagination-ellipsis';
                        pageNumbersDiv.appendChild(ellipsis);
                    }
                    addPageButton(pagination.total_pages);
                }
            }
            
            function addPageButton(pageNum) {
                const button = document.createElement('button');
                button.textContent = pageNum;
                button.className = 'pagination-btn' + (pageNum === currentPage ? ' active' : '');
                button.onclick = () => changePage(pageNum);
                document.getElementById('page-numbers').appendChild(button);
            }
            
            function changePage(page) {
                if (page >= 1 && page <= pagination.total_pages && page !== currentPage) {
                    notifyUserActivity(); // Track pagination interaction
                    loadPhotos(page);
                }
            }

            function jumpToPage(event) {
                event.preventDefault();
                const input = document.getElementById('page-jump-input');
                const requestedPage = parseInt(input.value, 10);
                if (!Number.isInteger(requestedPage)) {
                    input.value = currentPage;
                    return;
                }
                const targetPage = Math.min(Math.max(requestedPage, 1), pagination.total_pages);
                if (targetPage === currentPage) {
                    input.value = currentPage;
                    return;
                }
                changePage(targetPage);
            }

            function updatePageUrl() {
                const url = new URL(window.location.href);
                if (currentPage === 1) {
                    url.searchParams.delete('page');
                } else {
                    url.searchParams.set('page', currentPage);
                }
                window.history.replaceState({}, '', url);
            }

            function setupPhotoCardHandlers() {
                const photoCards = document.querySelectorAll('.photo-card');
                
                photoCards.forEach(card => {
                    let tapTimeout;
                    let lastTap = 0;
                    
                    // Handle click/tap events
                    card.addEventListener('click', function(e) {
                        const currentTime = new Date().getTime();
                        const tapLength = currentTime - lastTap;
                        
                        // Check if this is a touch device
                        const isTouchDevice = 'ontouchstart' in window || navigator.maxTouchPoints > 0;
                        
                        if (isTouchDevice) {
                            // On mobile: first tap shows buttons, second tap (double-tap) opens photo
                            if (tapLength < 500 && tapLength > 0) {
                                // Double tap - open photo
                                const photoId = card.getAttribute('data-photo-id');
                                viewPhoto(photoId);
                                card.classList.remove('active');
                            } else {
                                // Single tap - toggle buttons
                                clearTimeout(tapTimeout);
                                tapTimeout = setTimeout(() => {
                                    // Remove active class from all other cards
                                    photoCards.forEach(otherCard => {
                                        if (otherCard !== card) {
                                            otherCard.classList.remove('active');
                                        }
                                    });
                                    // Toggle this card
                                    card.classList.toggle('active');
                                }, 300);
                            }
                            lastTap = currentTime;
                        } else {
                            // On desktop: single click opens photo (hover shows buttons)
                            const photoId = card.getAttribute('data-photo-id');
                            viewPhoto(photoId);
                        }
                    });
                    
                    // Close buttons when clicking outside on mobile
                    document.addEventListener('click', function(e) {
                        if (!card.contains(e.target)) {
                            card.classList.remove('active');
                        }
                    });
                });
            }
            
            function formatFileSize(bytes) {
                const units = ['B', 'KB', 'MB', 'GB'];
                let size = bytes;
                let unitIndex = 0;
                
                while (size >= 1024 && unitIndex < units.length - 1) {
                    size /= 1024;
                    unitIndex++;
                }
                
                return `${size.toFixed(1)} ${units[unitIndex]}`;
            }
            
            function viewPhoto(photoId) {
                notifyUserActivity(); // Track photo viewing
                const photo = photos.find(p => p.id === photoId);
                if (photo) {
                    // Show dithered version if available, otherwise show original
                    const imageToShow = photo.has_dithered ? photo.dithered_path : photo.original_path;
                    window.open(imageToShow, '_blank');
                }
            }
            
            async function displayPhoto(photoId) {
                try {
                    notifyUserActivity(); // Track display interaction
                    const response = await fetch(`/api/display/${photoId}`, {
                        method: 'POST'
                    });
                    
                    if (response.ok) {
                        const result = await response.json();
                        alert(result.message);
                    } else {
                        const error = await response.json();
                        alert(`Error: ${error.detail}`);
                    }
                } catch (error) {
                    console.error('Error displaying photo:', error);
                    alert('Error displaying photo');
                }
            }

            async function showDashboardQr() {
                try {
                    notifyUserActivity();
                    const response = await fetch('/api/dashboard/qr', {
                        method: 'POST'
                    });
                    const result = await response.json();
                    if (response.ok && result.success) {
                        alert(result.message || 'Dashboard QR displayed');
                    } else {
                        alert(`Error: ${result.detail || result.message || 'Could not show dashboard QR'}`);
                    }
                } catch (error) {
                    console.error('Error showing dashboard QR:', error);
                    alert('Error showing dashboard QR');
                }
            }

            async function runExtensionAction(extensionId, photoId, button) {
                const originalText = button ? button.textContent : '';
                try {
                    notifyUserActivity();
                    if (button) {
                        button.textContent = 'uploading...';
                        button.disabled = true;
                    }

                    const response = await fetch(`/api/extensions/${extensionId}/photos/${photoId}`, {
                        method: 'POST'
                    });
                    const result = await response.json();

                    if (response.ok) {
                        alert(result.message || 'Upload complete');
                    } else {
                        alert(`Error: ${result.detail || 'Upload failed'}`);
                    }
                } catch (error) {
                    console.error('Error running extension action:', error);
                    alert('Error running extension action');
                } finally {
                    if (button) {
                        button.textContent = originalText;
                        button.disabled = false;
                    }
                }
            }
            
            async function capturePhoto() {
                try {
                    notifyUserActivity(); // Track capture interaction
                    // Find the capture button and show loading indicator
                    const captureBtn = document.querySelector('button[onclick="capturePhoto()"]');
                    if (captureBtn) {
                        captureBtn.textContent = 'capturing...';
                        captureBtn.disabled = true;
                    }
                    
                    const response = await fetch('/api/capture', {
                        method: 'POST'
                    });
                    
                    if (response.ok) {
                        const result = await response.json();
                        alert(result.message);
                        loadPhotos(currentPage);
                    } else {
                        const error = await response.json();
                        alert(`Error: ${error.detail}`);
                    }
                } catch (error) {
                    console.error('Error capturing photo:', error);
                    alert('Error capturing photo');
                } finally {
                    // Restore button state
                    const captureBtn = document.querySelector('button[onclick="capturePhoto()"]');
                    if (captureBtn) {
                        captureBtn.textContent = 'capture photo';
                        captureBtn.disabled = false;
                    }
                }
            }
            
            async function reprocessPhoto(photoId) {
                try {
                    const response = await fetch(`/api/reprocess/${photoId}`, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        }
                    });
                    
                    if (response.ok) {
                        const result = await response.json();
                        alert(result.message);
                        // Refresh gallery to show updated photo (stay on current page)
                        loadPhotos(currentPage);
                    } else {
                        const error = await response.json();
                        alert(`Error: ${error.detail}`);
                    }
                } catch (error) {
                    console.error('Error reprocessing photo:', error);
                    alert('Error reprocessing photo');
                }
            }
            
            async function openSettings() {
                try {
                    notifyUserActivity(); // Track settings interaction
                    const response = await fetch('/api/settings');
                    const settings = await response.json();
                    populateSettingsForm(settings);
                    loadRecipes();
                    await loadCameraLimits();
                    toggleManualExposureFields();
                    document.getElementById('settings-modal').style.display = 'block';
                    lockSettingsPageScroll();
                } catch (error) {
                    console.error('Error loading settings:', error);
                    alert('Error loading settings');
                }
            }
            
            function closeSettings(force = false) {
                if (!force && hasUnsavedSettings()) {
                    const shouldClose = confirm('You have unsaved settings. Close without saving?');
                    if (!shouldClose) {
                        return false;
                    }
                }
                document.getElementById('settings-modal').style.display = 'none';
                unlockSettingsPageScroll();
                settingsFormSnapshot = null;
                return true;
            }

            function lockSettingsPageScroll() {
                if (document.body.classList.contains('settings-open')) {
                    return;
                }
                settingsPageScrollY = window.scrollY;
                document.documentElement.classList.add('settings-open');
                document.body.classList.add('settings-open');
                document.body.style.top = `-${settingsPageScrollY}px`;
            }

            function unlockSettingsPageScroll() {
                if (!document.body.classList.contains('settings-open')) {
                    return;
                }
                document.documentElement.classList.remove('settings-open');
                document.body.classList.remove('settings-open');
                document.body.style.top = '';
                window.scrollTo(0, settingsPageScrollY);
            }

            function getSettingsFormSnapshot() {
                const controls = document.querySelectorAll(
                    '#settings-modal input.setting-input, #settings-modal select.setting-input'
                );
                return JSON.stringify({
                    values: Array.from(controls).map(control => [control.id, control.value]),
                    arenaTokenShouldClear
                });
            }

            function hasUnsavedSettings() {
                return settingsFormSnapshot !== null
                    && settingsFormSnapshot !== getSettingsFormSnapshot();
            }
            
            function populateSettingsForm(settings) {
                // Camera settings
                document.getElementById('resolution-width').value = settings.camera.resolution.width;
                document.getElementById('resolution-height').value = settings.camera.resolution.height;
                document.getElementById('exposure-value').value = settings.camera.exposure_value;
                document.getElementById('sharpness').value = settings.camera.sharpness;
                document.getElementById('autofocus-mode').value = settings.camera.autofocus_mode;
                document.getElementById('recipe-exposure-mode').value = settings.camera.exposure_mode || 'auto';
                document.getElementById('recipe-exposure-time').value = (settings.camera.exposure_time_us || 0) / 1000000;
                document.getElementById('recipe-analogue-gain').value = settings.camera.analogue_gain || 1.0;
                toggleManualExposureFields();

                // Processing settings
                document.getElementById('saturation').value = settings.processing.saturation;
                document.getElementById('brightness-factor').value = settings.processing.brightness_factor;
                document.getElementById('color-factor').value = settings.processing.color_factor;
                document.getElementById('dithering-method').value = settings.processing.dithering_method;
                document.getElementById('bayer-size').value = settings.processing.bayer_size || 4;
                document.getElementById('threshold-scale').value = settings.processing.threshold_scale || 1.0;
                
                // Show/hide ordered dithering settings
                toggleOrderedSettings();
                
                // System settings
                document.getElementById('camera-name').value = settings.system.camera_name || '';
                document.getElementById('auto-refresh-interval').value = settings.system.auto_refresh_interval;
                document.getElementById('auto-timeout-enabled').value = settings.system.auto_timeout_enabled ? 'true' : 'false';
                document.getElementById('auto-timeout-minutes').value = settings.system.auto_timeout_minutes || 10;
                document.getElementById('show-dashboard-qr-on-wifi-connect').value = settings.system.show_dashboard_qr_on_wifi_connect !== false ? 'true' : 'false';
                const exportSettings = settings.exports || {};
                document.getElementById('upscale-dithered-2x').value = exportSettings.upscale_dithered_2x ? 'true' : 'false';
                document.getElementById('update-status').textContent = 'Updates code, dependencies, and service files while preserving settings and photos.';
                document.getElementById('update-install-btn').style.display = 'none';

                const arenaSettings = (settings.extensions && settings.extensions.arena) || {};
                document.getElementById('arena-enabled').value = arenaSettings.enabled ? 'true' : 'false';
                document.getElementById('arena-channel').value = arenaSettings.channel || '';
                document.getElementById('arena-access-token').value = '';
                arenaTokenShouldClear = false;
                updateArenaTokenStatus(Boolean(arenaSettings.access_token_configured));

                const trigger = settings.trigger || {};
                const bracket = trigger.bracket || {};
                document.getElementById('trigger-program').value = trigger.program || 'single';
                document.getElementById('trigger-bracket-frames').value = String(bracket.frames || 3);
                document.getElementById('trigger-bracket-step').value = bracket.step_ev || 1.0;
                toggleBracketFields();
                settingsFormSnapshot = getSettingsFormSnapshot();
            }

            function updateArenaTokenStatus(configured) {
                const status = document.getElementById('arena-token-status');
                const clearBtn = document.getElementById('arena-clear-token-btn');
                if (arenaTokenShouldClear) {
                    status.textContent = 'Token will be cleared when settings are saved';
                    clearBtn.disabled = true;
                } else if (configured) {
                    status.textContent = 'Token saved. Leave blank to keep it.';
                    clearBtn.disabled = false;
                } else {
                    status.textContent = 'No token configured';
                    clearBtn.disabled = true;
                }
            }

            function clearArenaToken() {
                arenaTokenShouldClear = true;
                document.getElementById('arena-access-token').value = '';
                updateArenaTokenStatus(false);
            }

            function updateSoftwareStatus(data) {
                const status = document.getElementById('update-status');
                const installBtn = document.getElementById('update-install-btn');
                status.textContent = data.message || 'Update status checked.';
                installBtn.style.display = data.can_update ? 'inline-block' : 'none';
            }

            async function checkForUpdates() {
                const status = document.getElementById('update-status');
                const checkBtn = document.getElementById('update-check-btn');
                const installBtn = document.getElementById('update-install-btn');

                try {
                    checkBtn.disabled = true;
                    installBtn.style.display = 'none';
                    status.textContent = 'Checking for updates...';
                    const response = await fetch('/api/update/status');
                    const data = await response.json().catch(() => ({}));

                    if (response.ok) {
                        updateSoftwareStatus(data);
                    } else {
                        status.textContent = data.detail || 'Could not check for updates.';
                    }
                } catch (error) {
                    console.error('Error checking for updates:', error);
                    status.textContent = 'Could not check for updates.';
                } finally {
                    checkBtn.disabled = false;
                }
            }

            async function installUpdate() {
                const status = document.getElementById('update-status');
                const checkBtn = document.getElementById('update-check-btn');
                const installBtn = document.getElementById('update-install-btn');

                if (!confirm('Install the available update? The camera should be rebooted after the update finishes.')) {
                    return;
                }

                try {
                    checkBtn.disabled = true;
                    installBtn.disabled = true;
                    status.textContent = 'Installing update...';
                    const response = await fetch('/api/update/install', { method: 'POST' });
                    const data = await response.json().catch(() => ({}));

                    if (response.ok) {
                        status.textContent = data.message || 'Update installed. Reboot the camera to finish.';
                        installBtn.style.display = 'none';
                    } else {
                        status.textContent = data.detail || 'Could not install update.';
                        if (data.can_update) {
                            installBtn.style.display = 'inline-block';
                        }
                    }
                } catch (error) {
                    console.error('Error installing update:', error);
                    status.textContent = 'Could not install update.';
                } finally {
                    checkBtn.disabled = false;
                    installBtn.disabled = false;
                }
            }
            
            async function saveSettings() {
                try {
                    const settings = {
                        camera: {
                            resolution: {
                                width: parseInt(document.getElementById('resolution-width').value),
                                height: parseInt(document.getElementById('resolution-height').value)
                            },
                            exposure_value: parseFloat(document.getElementById('exposure-value').value),
                            sharpness: parseInt(document.getElementById('sharpness').value),
                            autofocus_mode: parseInt(document.getElementById('autofocus-mode').value)
                        },
                        processing: {
                            saturation: parseFloat(document.getElementById('saturation').value),
                            brightness_factor: parseFloat(document.getElementById('brightness-factor').value),
                            color_factor: parseFloat(document.getElementById('color-factor').value),
                            dithering_method: document.getElementById('dithering-method').value,
                            bayer_size: parseInt(document.getElementById('bayer-size').value),
                            threshold_scale: parseFloat(document.getElementById('threshold-scale').value)
                        },
                        system: {
                            camera_name: document.getElementById('camera-name').value.trim(),
                            auto_refresh_interval: parseInt(document.getElementById('auto-refresh-interval').value),
                            auto_timeout_enabled: document.getElementById('auto-timeout-enabled').value === 'true',
                            auto_timeout_minutes: parseInt(document.getElementById('auto-timeout-minutes').value),
                            show_dashboard_qr_on_wifi_connect: document.getElementById('show-dashboard-qr-on-wifi-connect').value === 'true'
                        },
                        exports: {
                            upscale_dithered_2x: document.getElementById('upscale-dithered-2x').value === 'true'
                        },
                        trigger: {
                            program: document.getElementById('trigger-program').value,
                            bracket: {
                                frames: Number(document.getElementById('trigger-bracket-frames').value),
                                step_ev: Number(document.getElementById('trigger-bracket-step').value)
                            }
                        },
                        extensions: {
                            arena: {
                                enabled: document.getElementById('arena-enabled').value === 'true',
                                channel: document.getElementById('arena-channel').value.trim(),
                                access_token: document.getElementById('arena-access-token').value.trim(),
                                access_token_clear: arenaTokenShouldClear
                            }
                        }
                    };
                    
                    const response = await fetch('/api/settings', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify(settings)
                    });
                    
                    if (response.ok) {
                        settingsFormSnapshot = getSettingsFormSnapshot();
                        alert('Settings saved successfully!');
                        closeSettings(true);
                        // Update auto-refresh interval if changed
                        updateAutoRefreshInterval();
                        await loadExtensionActions();
                        renderGallery();
                    } else {
                        alert('Error saving settings');
                    }
                } catch (error) {
                    console.error('Error saving settings:', error);
                    alert('Error saving settings');
                }
            }
            
            async function resetSettings() {
                if (confirm('Are you sure you want to reset all settings to defaults?')) {
                    try {
                        // The server owns the default values (including the built-in
                        // recipe set), so the reset is delegated to it rather than
                        // assembling a defaults payload here.
                        const response = await fetch('/api/settings/reset', {
                            method: 'POST'
                        });

                        if (response.ok) {
                            const settings = await (await fetch('/api/settings')).json();
                            populateSettingsForm(settings);
                            await loadRecipes();
                            await loadExtensionActions();
                            renderGallery();
                            alert('Settings reset to defaults!');
                        } else {
                            alert('Error resetting settings');
                        }
                    } catch (error) {
                        console.error('Error resetting settings:', error);
                        alert('Error resetting settings');
                    }
                }
            }
            
            let autoRefreshInterval;
            
            function updateAutoRefreshInterval() {
                // Clear existing interval
                if (autoRefreshInterval) {
                    clearInterval(autoRefreshInterval);
                }
                
                // Get current auto-refresh setting
                fetch('/api/settings')
                    .then(response => response.json())
                    .then(settings => {
                        const intervalSeconds = settings.system.auto_refresh_interval;
                        if (intervalSeconds > 0) {
                            autoRefreshInterval = setInterval(
                                () => loadPhotos(currentPage),
                                intervalSeconds * 1000
                            );
                        }
                    })
                    .catch(error => console.error('Error updating auto-refresh:', error));
            }
            
            function toggleOrderedSettings() {
                const ditheringMethod = document.getElementById('dithering-method').value;
                const bayerSettings = document.getElementById('bayer-settings');
                const thresholdSettings = document.getElementById('threshold-settings');
                
                if (ditheringMethod === 'ordered') {
                    bayerSettings.style.display = 'block';
                    thresholdSettings.style.display = 'block';
                } else {
                    bayerSettings.style.display = 'none';
                    thresholdSettings.style.display = 'none';
                }
            }
            
            function refreshGallery() {
                notifyUserActivity(); // Track refresh interaction
                loadPhotos(currentPage);
            }

            function formatDownloadSize(bytes) {
                if (!Number.isFinite(bytes) || bytes <= 0) {
                    return '';
                }
                const units = ['B', 'KB', 'MB', 'GB'];
                const unitIndex = Math.min(
                    Math.floor(Math.log(bytes) / Math.log(1024)),
                    units.length - 1
                );
                const value = bytes / Math.pow(1024, unitIndex);
                return `${value.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
            }

            function resetDownloadButton(downloadBtn) {
                downloadBtn.dataset.ready = 'false';
                delete downloadBtn.dataset.downloadSize;
                downloadBtn.textContent = 'download all photos';
                downloadBtn.disabled = false;
                downloadBtn.style.opacity = '1';
            }

            function handleDownloadError(downloadBtn, abortBtn, error) {
                console.error('Error downloading photos:', error);
                alert(`Error: ${error.message}`);
                if (downloadBtn) {
                    resetDownloadButton(downloadBtn);
                }
                if (abortBtn) {
                    abortBtn.style.display = 'none';
                }
            }

            function startPreparedZipDownload(downloadBtn, size = '') {
                downloadBtn.dataset.ready = 'false';
                downloadBtn.textContent = size
                    ? `downloading ${size} in browser...`
                    : 'downloading in browser...';
                downloadBtn.disabled = true;
                downloadBtn.style.opacity = '0.7';

                const link = document.createElement('a');
                link.href = '/api/photos/download-all/result';
                link.download = `reframe-photos-${new Date().toISOString().split('T')[0]}.zip`;
                link.style.display = 'none';
                document.body.appendChild(link);
                link.click();
                link.remove();

                monitorBrowserDownload(downloadBtn);
            }

            async function monitorBrowserDownload(downloadBtn, attempts = 0) {
                try {
                    const response = await fetch('/api/photos/download-all/progress');
                    if (!response.ok) {
                        throw new Error('Could not check download status');
                    }
                    const progress = await response.json();

                    if (progress.status === 'idle') {
                        downloadBtn.textContent = 'download sent to browser';
                        setTimeout(() => resetDownloadButton(downloadBtn), 2000);
                        return;
                    }
                    if (progress.status === 'completed' && attempts >= 3) {
                        const size = formatDownloadSize(progress.size_bytes);
                        downloadBtn.textContent = size
                            ? `download ZIP (${size})`
                            : 'download ZIP';
                        downloadBtn.dataset.ready = 'true';
                        downloadBtn.dataset.downloadSize = size;
                        downloadBtn.disabled = false;
                        downloadBtn.style.opacity = '1';
                        return;
                    }
                    if (progress.status === 'error' || progress.status === 'aborted') {
                        throw new Error(progress.message || 'Download failed');
                    }
                    if (attempts >= 600) {
                        throw new Error('Browser download timed out');
                    }

                    setTimeout(() => monitorBrowserDownload(downloadBtn, attempts + 1), 1000);
                } catch (error) {
                    console.error('Browser download error:', error);
                    downloadBtn.textContent = 'download failed';
                    setTimeout(() => resetDownloadButton(downloadBtn), 2500);
                }
            }
            
            async function downloadAllPhotos() {
                try {
                    notifyUserActivity(); // Track download interaction
                    
                    // Find the download button and show loading state
                    const downloadBtn = document.querySelector('button[onclick="downloadAllPhotos()"]');
                    const abortBtn = document.getElementById('abort-btn');
                    if (downloadBtn && downloadBtn.dataset.ready === 'true') {
                        startPreparedZipDownload(downloadBtn, downloadBtn.dataset.downloadSize || '');
                        return;
                    }
                    if (downloadBtn) {
                        downloadBtn.textContent = 'starting download...';
                        downloadBtn.disabled = true;
                        downloadBtn.style.opacity = '0.7';
                    }
                    
                    // Show abort button
                    if (abortBtn) {
                        abortBtn.style.display = 'inline-block';
                    }
                    
                    // Start the download process
                    const startResponse = await fetch('/api/photos/download-all/start', {
                        method: 'POST'
                    });
                    
                    if (!startResponse.ok) {
                        const error = await startResponse.json();
                        throw new Error(error.detail || 'Failed to start download');
                    }
                    
                    const startData = await startResponse.json();
                    const totalPhotos = startData.total_photos;
                    
                    // Poll for progress
                    let attempts = 0;
                    const maxAttempts = 600; // 10 minutes max
                    
                    window.progressInterval = setInterval(async () => {
                        attempts++;
                        
                        try {
                            const progressResponse = await fetch('/api/photos/download-all/progress');
                            if (progressResponse.ok) {
                                const progress = await progressResponse.json();
                                
                                if (downloadBtn) {
                                    if (progress.status === 'creating') {
                                        const percent = Math.round((progress.processed / progress.total) * 100);
                                        downloadBtn.textContent = `${progress.message} (${percent}%)`;
                                    } else if (progress.status === 'completed') {
                                        clearInterval(window.progressInterval);
                                        window.progressInterval = null;
                                        
                                        // Hide abort button
                                        if (abortBtn) {
                                            abortBtn.style.display = 'none';
                                        }

                                        const size = formatDownloadSize(progress.size_bytes);
                                        startPreparedZipDownload(downloadBtn, size);
                                    } else if (progress.status === 'aborted') {
                                        clearInterval(window.progressInterval);
                                        window.progressInterval = null;
                                        downloadBtn.textContent = 'download aborted';
                                        setTimeout(() => {
                                            if (downloadBtn) {
                                                resetDownloadButton(downloadBtn);
                                            }
                                            if (abortBtn) {
                                                abortBtn.style.display = 'none';
                                            }
                                        }, 2000);
                                        return;
                                    } else if (progress.status === 'error') {
                                        throw new Error(progress.message);
                                    }
                                }
                            } else {
                                throw new Error('Failed to get progress');
                            }
                        } catch (error) {
                            clearInterval(window.progressInterval);
                            window.progressInterval = null;
                            handleDownloadError(downloadBtn, abortBtn, error);
                            return;
                        }
                        
                        // Timeout after max attempts
                        if (attempts >= maxAttempts) {
                            clearInterval(window.progressInterval);
                            window.progressInterval = null;
                            handleDownloadError(
                                downloadBtn,
                                abortBtn,
                                new Error('Download timed out after 10 minutes')
                            );
                        }
                    }, 1000); // Check progress every second
                    
                } catch (error) {
                    const downloadBtn = document.querySelector('button[onclick="downloadAllPhotos()"]');
                    const abortBtn = document.getElementById('abort-btn');
                    handleDownloadError(downloadBtn, abortBtn, error);
                }
            }
            
            async function abortDownload() {
                try {
                    const response = await fetch('/api/photos/download-all/abort', {
                        method: 'POST'
                    });
                    
                    if (response.ok) {
                        const abortBtn = document.getElementById('abort-btn');
                        const downloadBtn = document.querySelector('button[onclick="downloadAllPhotos()"]');
                        
                        if (abortBtn) {
                            abortBtn.textContent = 'aborting...';
                            abortBtn.disabled = true;
                        }
                        
                        // Clear any existing progress interval
                        if (window.progressInterval) {
                            clearInterval(window.progressInterval);
                            window.progressInterval = null;
                        }
                        
                        // Reset download button after a short delay
                        setTimeout(() => {
                            if (downloadBtn) {
                                downloadBtn.textContent = 'download all photos';
                                downloadBtn.disabled = false;
                                downloadBtn.style.opacity = '1';
                            }
                            if (abortBtn) {
                                abortBtn.style.display = 'none';
                            }
                        }, 1000);
                        
                    } else {
                        alert('Failed to abort download');
                    }
                } catch (error) {
                    console.error('Error aborting download:', error);
                    alert('Error aborting download');
                }
            }
            
            async function deleteAllPhotos() {
                const confirmed = confirm('⚠️ This will permanently delete ALL photos from the system. This action cannot be undone. Are you absolutely sure?');
                if (!confirmed) {
                    return;
                }
                
                const doubleConfirmed = confirm('Final confirmation: Delete ALL photos? This will remove both original and dithered versions.');
                if (!doubleConfirmed) {
                    return;
                }
                
                try {
                    notifyUserActivity(); // Track delete interaction
                    
                    // Find the delete button and show loading state
                    const deleteBtn = document.querySelector('button[onclick="deleteAllPhotos()"]');
                    const abortDeleteBtn = document.getElementById('abort-delete-btn');
                    if (deleteBtn) {
                        const originalText = deleteBtn.textContent;
                        deleteBtn.textContent = 'starting deletion...';
                        deleteBtn.disabled = true;
                        deleteBtn.style.opacity = '0.7';
                    }
                    
                    // Show abort button
                    if (abortDeleteBtn) {
                        abortDeleteBtn.style.display = 'inline-block';
                    }
                    
                    // Start the delete process
                    const startResponse = await fetch('/api/photos/delete-all/start', {
                        method: 'POST'
                    });
                    
                    if (!startResponse.ok) {
                        const error = await startResponse.json();
                        throw new Error(error.detail || 'Failed to start deletion');
                    }
                    
                    const startData = await startResponse.json();
                    
                    // If no photos to delete, show message and return
                    if (startData.status === 'completed') {
                        alert(startData.message || 'No photos to delete');
                        if (deleteBtn) {
                            deleteBtn.textContent = originalText;
                            deleteBtn.disabled = false;
                            deleteBtn.style.opacity = '1';
                        }
                        if (abortDeleteBtn) {
                            abortDeleteBtn.style.display = 'none';
                        }
                        return;
                    }
                    
                    const totalPhotos = startData.total_photos;
                    
                    // Poll for progress
                    let attempts = 0;
                    const maxAttempts = 300; // 5 minutes max
                    
                    window.deleteProgressInterval = setInterval(async () => {
                        attempts++;
                        
                        try {
                            const progressResponse = await fetch('/api/photos/delete-all/progress');
                            if (progressResponse.ok) {
                                const progress = await progressResponse.json();
                                
                                if (deleteBtn) {
                                    if (progress.status === 'deleting') {
                                        const percent = Math.round((progress.processed / progress.total) * 100);
                                        deleteBtn.textContent = `${progress.message} (${percent}%)`;
                                    } else if (progress.status === 'completed') {
                                        deleteBtn.textContent = 'deletion complete!';
                                        clearInterval(window.deleteProgressInterval);
                                        window.deleteProgressInterval = null;
                                        
                                        // Hide abort button
                                        if (abortDeleteBtn) {
                                            abortDeleteBtn.style.display = 'none';
                                        }
                                        
                                        // Show success message and refresh gallery
                                        alert(progress.message || 'All photos deleted successfully');
                                        loadPhotos(1);
                                        
                                        setTimeout(() => {
                                            if (deleteBtn) {
                                                deleteBtn.textContent = originalText;
                                                deleteBtn.disabled = false;
                                                deleteBtn.style.opacity = '1';
                                            }
                                        }, 2000);
                                    } else if (progress.status === 'aborted') {
                                        clearInterval(window.deleteProgressInterval);
                                        window.deleteProgressInterval = null;
                                        deleteBtn.textContent = 'deletion aborted';
                                        setTimeout(() => {
                                            if (deleteBtn) {
                                                deleteBtn.textContent = originalText;
                                                deleteBtn.disabled = false;
                                                deleteBtn.style.opacity = '1';
                                            }
                                            if (abortDeleteBtn) {
                                                abortDeleteBtn.style.display = 'none';
                                            }
                                        }, 2000);
                                        return;
                                    } else if (progress.status === 'error') {
                                        throw new Error(progress.message);
                                    }
                                }
                            } else {
                                throw new Error('Failed to get progress');
                            }
                        } catch (error) {
                            clearInterval(window.deleteProgressInterval);
                            window.deleteProgressInterval = null;
                            throw error;
                        }
                        
                        // Timeout after max attempts
                        if (attempts >= maxAttempts) {
                            clearInterval(window.deleteProgressInterval);
                            window.deleteProgressInterval = null;
                            throw new Error('Deletion timed out after 5 minutes');
                        }
                    }, 1000); // Check progress every second
                    
                } catch (error) {
                    console.error('Error deleting photos:', error);
                    alert(`Error: ${error.message}`);
                    
                    // Reset button on error
                    const deleteBtn = document.querySelector('button[onclick="deleteAllPhotos()"]');
                    const abortDeleteBtn = document.getElementById('abort-delete-btn');
                    if (deleteBtn) {
                        deleteBtn.textContent = 'delete all photos';
                        deleteBtn.disabled = false;
                        deleteBtn.style.opacity = '1';
                    }
                    if (abortDeleteBtn) {
                        abortDeleteBtn.style.display = 'none';
                    }
                }
            }
            
            async function abortDelete() {
                try {
                    const response = await fetch('/api/photos/delete-all/abort', {
                        method: 'POST'
                    });
                    
                    if (response.ok) {
                        const abortDeleteBtn = document.getElementById('abort-delete-btn');
                        const deleteBtn = document.querySelector('button[onclick="deleteAllPhotos()"]');
                        
                        if (abortDeleteBtn) {
                            abortDeleteBtn.textContent = 'aborting...';
                            abortDeleteBtn.disabled = true;
                        }
                        
                        // Clear any existing progress interval
                        if (window.deleteProgressInterval) {
                            clearInterval(window.deleteProgressInterval);
                            window.deleteProgressInterval = null;
                        }
                        
                        // Reset delete button after a short delay
                        setTimeout(() => {
                            if (deleteBtn) {
                                deleteBtn.textContent = 'delete all photos';
                                deleteBtn.disabled = false;
                                deleteBtn.style.opacity = '1';
                            }
                            if (abortDeleteBtn) {
                                abortDeleteBtn.style.display = 'none';
                            }
                        }, 1000);
                        
                    } else {
                        alert('Failed to abort deletion');
                    }
                } catch (error) {
                    console.error('Error aborting deletion:', error);
                    alert('Error aborting deletion');
                }
            }
            
            // Load photos on page load
            document.addEventListener('DOMContentLoaded', function() {
                loadPhotos(currentPage);
                updateAutoRefreshInterval();
                updateBatteryLevel();
                
                // Add event listener for dithering method changes
                document.getElementById('dithering-method').addEventListener('change', toggleOrderedSettings);
                
                // Update battery level every 30 seconds
                setInterval(updateBatteryLevel, 30000);
            });

            window.addEventListener('beforeunload', function(event) {
                const modal = document.getElementById('settings-modal');
                if (modal.style.display === 'block' && hasUnsavedSettings()) {
                    event.preventDefault();
                    event.returnValue = '';
                }
            });
            
            async function updateBatteryLevel() {
                try {
                    const response = await fetch('/api/battery');
                    if (response.ok) {
                        const data = await response.json();
                        const batteryElement = document.getElementById('battery-level');
                        
                        if (data.battery_level !== null && data.battery_level !== undefined) {
                            batteryElement.textContent = `battery: ${data.battery_level}%`;
                            
                            // Add visual indicator based on battery level
                            batteryElement.className = '';
                            if (data.battery_level <= 10) {
                                batteryElement.style.color = '#d32f2f'; // Red for low battery
                            } else if (data.battery_level <= 25) {
                                batteryElement.style.color = '#f57c00'; // Orange for warning
                            } else {
                                batteryElement.style.color = ''; // Default color
                            }
                        } else {
                            batteryElement.textContent = 'battery: --%';
                            batteryElement.style.color = '#666'; // Gray for unknown
                        }
                    }
                } catch (error) {
                    console.log('Could not fetch battery level:', error);
                    const batteryElement = document.getElementById('battery-level');
                    batteryElement.textContent = 'battery: --%';
                    batteryElement.style.color = '#666';
                }
            }
            
            async function loadRecipes() {
                try {
                    const response = await fetch('/api/recipes');
                    if (!response.ok) {
                        throw new Error('Could not load recipes');
                    }
                    renderRecipes(await response.json());
                    attachRecipeListeners();
                } catch (error) {
                    console.error('Error loading recipes:', error);
                    document.getElementById('recipe-status').textContent = 'Could not load recipes.';
                }
            }

            function renderRecipes(data) {
                const list = document.getElementById('recipe-list');
                list.innerHTML = data.items.map(recipe => {
                    const isActive = recipe.id === data.active;
                    return `
                        <div class="recipe-item ${isActive ? 'is-active' : ''}" data-recipe-id="${escapeAttr(recipe.id)}">
                            <span class="recipe-item-name">${escapeHtml(recipe.name || recipe.id)}</span>
                            <span class="recipe-item-actions">
                                <button type="button" class="action-btn btn-secondary recipe-use-btn"
                                    ${isActive ? 'disabled' : ''}>${isActive ? 'Active' : 'Use'}</button>
                                <button type="button" class="action-btn btn-secondary recipe-delete-btn"
                                    ${isActive || data.items.length <= 1 ? 'disabled' : ''}>Delete</button>
                            </span>
                        </div>`;
                }).join('');
            }

            function escapeAttr(value) {
                const div = document.createElement('div');
                div.textContent = value;
                return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
            }

            function escapeHtml(value) {
                const div = document.createElement('div');
                div.textContent = value;
                return div.innerHTML;
            }

            function attachRecipeListeners() {
                const list = document.getElementById('recipe-list');
                if (!list) return;
                // Remove any existing listener to avoid duplicates
                list.removeEventListener('click', handleRecipeClick);
                list.addEventListener('click', handleRecipeClick);
            }

            function handleRecipeClick(event) {
                const useBtn = event.target.closest('.recipe-use-btn');
                const deleteBtn = event.target.closest('.recipe-delete-btn');
                const item = event.target.closest('[data-recipe-id]');
                if (!item) return;
                const recipeId = item.dataset.recipeId;
                if (useBtn) {
                    activateRecipe(recipeId);
                } else if (deleteBtn) {
                    deleteRecipe(recipeId);
                }
            }

            async function activateRecipe(recipeId) {
                const succeeded = await sendRecipeRequest(`/api/recipes/${encodeURIComponent(recipeId)}/activate`, 'POST',
                    null, 'Recipe activated.');
                if (!succeeded) {
                    // Failure already surfaced in recipe-status. Repopulating the
                    // form here would wipe out any unsaved edits and disarm the
                    // unsaved-changes guard for no reason.
                    return;
                }
                // Activation rewrites the derived cache, so the form is now stale.
                const settings = await (await fetch('/api/settings')).json();
                populateSettingsForm(settings);
            }

            async function saveRecipe() {
                const name = document.getElementById('recipe-name').value.trim();
                if (!name) {
                    document.getElementById('recipe-status').textContent = 'Give the recipe a name first.';
                    return;
                }
                const recipe = {
                    id: name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''),
                    name: name,
                    capture: {
                        exposure_mode: document.getElementById('recipe-exposure-mode').value,
                        exposure_time_us: Math.round(
                            Number(document.getElementById('recipe-exposure-time').value) * 1000000),
                        analogue_gain: Number(document.getElementById('recipe-analogue-gain').value),
                        exposure_value: Number(document.getElementById('exposure-value').value),
                        sharpness: Number(document.getElementById('sharpness').value),
                        autofocus_mode: Number(document.getElementById('autofocus-mode').value)
                    },
                    render: {
                        saturation: Number(document.getElementById('saturation').value),
                        brightness_factor: Number(document.getElementById('brightness-factor').value),
                        color_factor: Number(document.getElementById('color-factor').value),
                        dithering_method: document.getElementById('dithering-method').value,
                        bayer_size: Number(document.getElementById('bayer-size').value),
                        threshold_scale: Number(document.getElementById('threshold-scale').value)
                    }
                };
                if (!recipe.id) {
                    document.getElementById('recipe-status').textContent = 'That name has no usable characters.';
                    return;
                }
                await sendRecipeRequest('/api/recipes', 'POST', recipe, 'Recipe saved.');
                document.getElementById('recipe-name').value = '';
            }

            let cameraLimits = null;

            // Bounded by the dashboard's own HTTP timeout (30s), not by the
            // sensor: a capture longer than that makes the dashboard report
            // failure while the photo is taken and displayed anyway.
            const MAX_ADVERTISED_EXPOSURE_SECONDS = 20;

            // Floor, not round: toFixed(1) rounds to nearest, which can
            // advertise a max (e.g. 11.8s for a true 11.767s ceiling) that is
            // not actually achievable.
            function flooredSecondsLabel(seconds) {
                return (Math.floor(seconds * 10) / 10).toFixed(1);
            }

            async function loadCameraLimits() {
                try {
                    const response = await fetch('/api/camera/limits');
                    if (!response.ok) {
                        // Camera process may be down; leave the inputs unbounded
                        // rather than inventing limits we cannot verify.
                        return;
                    }
                    cameraLimits = await response.json();
                    applyCameraLimits();
                } catch (error) {
                    console.error('Could not load camera limits:', error);
                }
            }

            function applyCameraLimits() {
                if (!cameraLimits) {
                    return;
                }
                const exposure = cameraLimits.exposure_time_us;
                const gain = cameraLimits.analogue_gain;

                const exposureInput = document.getElementById('recipe-exposure-time');
                const minSeconds = exposure.min / 1000000;
                const maxSeconds = Math.min(exposure.max / 1000000, MAX_ADVERTISED_EXPOSURE_SECONDS);
                exposureInput.min = minSeconds.toFixed(4);
                exposureInput.max = flooredSecondsLabel(maxSeconds);
                document.getElementById('recipe-exposure-time-range').textContent =
                    `sensor supports ${minSeconds.toFixed(4)}s to ${flooredSecondsLabel(maxSeconds)}s`;

                const gainInput = document.getElementById('recipe-analogue-gain');
                gainInput.min = gain.min;
                gainInput.max = gain.max;
                document.getElementById('recipe-analogue-gain-range').textContent =
                    `sensor supports ${gain.min}x to ${gain.max}x`;
            }

            function toggleManualExposureFields() {
                const manual = document.getElementById('recipe-exposure-mode').value === 'manual';
                document.querySelectorAll('.manual-exposure-field').forEach(field => {
                    field.classList.toggle('is-visible', manual);
                });
            }

            function toggleBracketFields() {
                const bracketing = document.getElementById('trigger-program').value === 'bracket';
                document.querySelectorAll('.bracket-field').forEach(field => {
                    field.classList.toggle('is-visible', bracketing);
                });
            }

            function renderProvenance(photo) {
                if (!photo.recipe_name && !photo.frame_label) {
                    return '';
                }
                const parts = [];
                if (photo.recipe_name) {
                    parts.push(escapeHtml(photo.recipe_name));
                }
                if (photo.frame_label && photo.frame_label !== '0EV') {
                    parts.push(escapeHtml(photo.frame_label));
                }
                return `<div class="photo-provenance">${parts.join(' · ')}</div>`;
            }

            async function developPhoto(photoId, recipeId) {
                try {
                    const response = await fetch(`/api/photos/${encodeURIComponent(photoId)}/develop`, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({recipe_id: recipeId})
                    });
                    const data = await response.json();
                    if (!response.ok) {
                        alert(data.detail || 'Could not develop photo.');
                        return false;
                    }
                    await loadPhotos();
                    return true;
                } catch (error) {
                    console.error('Develop failed:', error);
                    return false;
                }
            }

            async function deleteRecipe(recipeId) {
                if (!confirm('Delete this recipe?')) {
                    return;
                }
                await sendRecipeRequest(`/api/recipes/${encodeURIComponent(recipeId)}`, 'DELETE', null, 'Recipe deleted.');
            }

            async function sendRecipeRequest(url, method, body, successMessage) {
                const status = document.getElementById('recipe-status');
                try {
                    const options = { method: method };
                    if (body) {
                        options.headers = { 'Content-Type': 'application/json' };
                        options.body = JSON.stringify(body);
                    }
                    const response = await fetch(url, options);
                    const data = await response.json().catch(() => ({}));
                    if (!response.ok) {
                        status.textContent = data.detail || 'Recipe request failed.';
                        return false;
                    }
                    status.textContent = successMessage;
                    await loadRecipes();
                    return true;
                } catch (error) {
                    console.error('Recipe request failed:', error);
                    status.textContent = 'Recipe request failed.';
                    return false;
                }
            }

            // Close modal when clicking outside of it
            window.onclick = function(event) {
                const modal = document.getElementById('settings-modal');
                if (event.target === modal) {
                    closeSettings();
                }
            }
