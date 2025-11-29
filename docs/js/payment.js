let selectedPaymentMethod = '';

function selectPaymentMethod(method) {
    selectedPaymentMethod = method;
    
    // Update UI
    document.querySelectorAll('.payment-method').forEach(pm => {
        pm.classList.remove('active');
    });
    event.currentTarget.classList.add('active');
    
    // Show payment details
    showPaymentDetails(method);
}

function showPaymentDetails(method) {
    const detailsContainer = document.getElementById('payment-details');
    
    const paymentMethods = {
        bank: `
            <div class="payment-details">
                <h4>🏦 תשלום בהעברה בנקאית</h4>
                <div class="bank-details">
                    <p><strong>בנק:</strong> הפועלים</p>
                    <p><strong>סניף:</strong> כפר גנים (153)</p>
                    <p><strong>חשבון:</strong> 73462</p>
                    <p><strong>המוטב:</strong> קאופמן צביקה</p>
                    <p><strong>סכום:</strong> 39 ש"ח</p>
                </div>
            </div>
        `,
        paybox: `
            <div class="payment-details">
                <h4>💎 תשלום דרך פיבוקס</h4>
                <p>לחץ על הכפתור למעבר לדף התשלום:</p>
                <a href="https://links.payboxapp.com/1SNfaJ6XcYb" class="btn" target="_blank" style="background: var(--secondary);">
                    💰 שלם עכשיו עם פיבוקס
                </a>
            </div>
        `,
        bit: `
            <div class="payment-details">
                <h4>💎 תשלום דרך ביט</h4>
                <p>לחץ על הכפתור למעבר לדף התשלום:</p>
                <a href="https://www.bitpay.co.il/app/share-info?i=190693822888_19l4oyvE" class="btn" target="_blank" style="background: var(--primary);">
                    💳 שלם עכשיו עם ביט
                </a>
            </div>
        `,
        paypal: `
            <div class="payment-details">
                <h4>💎 תשלום דרך פייפאל</h4>
                <p>לחץ על הכפתור למעבר לדף התשלום:</p>
                <a href="https://paypal.me/osifdu" class="btn" target="_blank" style="background: #0070ba;">
                    🅿️ שלם עכשיו עם פייפאל
                </a>
            </div>
        `,
        telegram: `
            <div class="payment-details">
                <h4>💎 תשלום דרך טלגרם</h4>
                <p>שלח תשלום לכתובת TON הבאה:</p>
                <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin: 15px 0;">
                    <code style="word-break: break-all;">UQCr743gEr_nqV_0SBkSp3CtYS_15R3LDLBvLmKeEv7XdGvp</code>
                </div>
                <p>סכום: 39 ש"ח (או שווה ערך ב-TON)</p>
            </div>
        `
    };
    
    detailsContainer.innerHTML = paymentMethods[method] + `
        <div class="upload-section">
            <h4>📸 העלה צילום מסך של האישור</h4>
            <div class="upload-area" onclick="document.getElementById('proof-file').click()" 
                 ondragover="handleDragOver(event)" ondrop="handleFileDrop(event)">
                <div class="upload-icon">📁</div>
                <p>לחץ כדי לבחור קובץ או גרור לכאן</p>
                <p style="font-size: 0.9rem; opacity: 0.7;">תמונות: JPG, PNG, GIF (מקסימום 5MB)</p>
            </div>
            <input type="file" id="proof-file" accept="image/*" style="display: none;" onchange="handleFileSelect(event)">
            <div class="preview-image" id="preview-image"></div>
            <button class="btn" onclick="submitPayment()" style="margin-top: 20px; display: none;" id="submit-btn">
                ✅ שלח לאישור
            </button>
        </div>
    `;
}

let selectedFile = null;

function handleDragOver(event) {
    event.preventDefault();
    event.currentTarget.classList.add('dragover');
}

function handleFileDrop(event) {
    event.preventDefault();
    event.currentTarget.classList.remove('dragover');
    const files = event.dataTransfer.files;
    if (files.length > 0) {
        handleFile(files[0]);
    }
}

function handleFileSelect(event) {
    const file = event.target.files[0];
    if (file) {
        handleFile(file);
    }
}

function handleFile(file) {
    if (file.size > 5 * 1024 * 1024) {
        alert('הקובץ גדול מדי. מקסימום 5MB.');
        return;
    }
    
    if (!file.type.startsWith('image/')) {
        alert('נא לבחור קובץ תמונה בלבד.');
        return;
    }
    
    selectedFile = file;
    
    // Show preview
    const reader = new FileReader();
    reader.onload = function(e) {
        const preview = document.getElementById('preview-image');
        preview.innerHTML = `<img src="${e.target.result}" alt="Preview" style="max-width: 100%; border-radius: 8px;">`;
        preview.style.display = 'block';
        
        // Show submit button
        document.getElementById('submit-btn').style.display = 'inline-block';
    };
    reader.readAsDataURL(file);
}

async function submitPayment() {
    if (!selectedPaymentMethod || !selectedFile) {
        alert('נא לבחור שיטת תשלום ולהעלות צילום מסך.');
        return;
    }
    
    const user = JSON.parse(localStorage.getItem('telegram_user'));
    if (!user) {
        alert('נא להתחבר עם Telegram תחילה.');
        return;
    }
    
    const formData = new FormData();
    formData.append('user_id', user.id);
    formData.append('username', user.username || '');
    formData.append('first_name', user.first_name);
    formData.append('last_name', user.last_name || '');
    formData.append('payment_method', selectedPaymentMethod);
    formData.append('proof_image', selectedFile);
    
    try {
        const response = await fetch('/api/payment.php', {
            method: 'POST',
            body: formData
        });
        
        const result = await response.json();
        
        if (result.success) {
            document.getElementById('payment-section').innerHTML = `
                <div class="status-badge status-pending">
                    ✅ התשלום נשלח לאישור!
                </div>
                <p>קיבלנו את הבקשה שלך. נבדוק את התשלום ונישלח לך הודעה כאשר יאושר.</p>
                <p>מספר בקשה: ${result.payment_id}</p>
                <div style="margin-top: 20px;">
                    <p><strong>קבוצות הפרויקט:</strong></p>
                    <a href="https://chat.whatsapp.com/CCKTtCu9BFPHZTZC6L9Bdh" class="btn" target="_blank" style="background: #25D366;">
                        📱 קבוצת וואטסאפ
                    </a>
                    <a href="https://t.me/+HIzvM8sEgh1kNWY0" class="btn" target="_blank" style="background: #0088cc;">
                        📢 קבוצת טלגרם
                    </a>
                </div>
            `;
            
            // Send notification to admin
            sendAdminNotification(user, result.payment_id);
        } else {
            alert('שגיאה בשליחת הבקשה: ' + result.error);
        }
    } catch (error) {
        console.error('Error submitting payment:', error);
        alert('שגיאה בשליחת הבקשה. נא לנסות שוב.');
    }
}

function sendAdminNotification(user, paymentId) {
    // This would typically be handled by the backend
    console.log('Admin notification:', { user, paymentId });
}
