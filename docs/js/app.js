// Tab Navigation
function showTab(tabName) {
    document.querySelectorAll('.tab-content').forEach(tab => {
        tab.classList.remove('active');
    });
    
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.remove('active');
    });
    
    document.getElementById(tabName + '-tab').classList.add('active');
    event.currentTarget.classList.add('active');
}

// Telegram Login Functions
function onTelegramAuth(user) {
    console.log('Telegram user:', user);
    
    localStorage.setItem('telegram_user', JSON.stringify(user));
    
    document.getElementById('user-details').innerHTML = 
        `<strong>${user.first_name} ${user.last_name || ''}</strong><br>
         ID: ${user.id}${user.username ? '<br>@' + user.username : ''}`;
    
    document.getElementById('user-info').style.display = 'block';
    
    // Check if user has active payment
    checkUserStatus(user.id);
}

function logout() {
    localStorage.removeItem('telegram_user');
    document.getElementById('user-info').style.display = 'none';
    alert('התנתקת בהצלחה!');
}

// Check if user is already logged in
document.addEventListener('DOMContentLoaded', function() {
    const savedUser = localStorage.getItem('telegram_user');
    if (savedUser) {
        const user = JSON.parse(savedUser);
        document.getElementById('user-details').innerHTML = 
            `<strong>${user.first_name} ${user.last_name || ''}</strong><br>
             ID: ${user.id}${user.username ? '<br>@' + user.username : ''}`;
        document.getElementById('user-info').style.display = 'block';
        checkUserStatus(user.id);
    }
    
    // Initialize FAQ
    document.querySelectorAll('.faq-question').forEach(question => {
        question.addEventListener('click', () => {
            const answer = question.nextElementSibling;
            answer.style.display = answer.style.display === 'block' ? 'none' : 'block';
        });
    });
});

// Check user payment status
async function checkUserStatus(userId) {
    try {
        const response = await fetch(`/api/user.php?action=status&user_id=${userId}`);
        const data = await response.json();
        
        if (data.status === 'approved') {
            showDashboard(data.userData);
        } else if (data.status === 'pending') {
            showPaymentStatus('pending', 'התשלום שלך נמצא בביקורת');
        } else {
            showPaymentOptions();
        }
    } catch (error) {
        console.error('Error checking user status:', error);
    }
}

function showPaymentOptions() {
    document.getElementById('payment-section').style.display = 'block';
    document.getElementById('dashboard-section').style.display = 'none';
}

function showPaymentStatus(status, message) {
    const statusSection = document.getElementById('payment-status');
    statusSection.innerHTML = `
        <div class="status-badge status-${status}">${message}</div>
        <p>נשלח אליך הודעה כאשר התשלום יאושר.</p>
    `;
    statusSection.style.display = 'block';
}

function showDashboard(userData) {
    document.getElementById('payment-section').style.display = 'none';
    document.getElementById('dashboard-section').style.display = 'block';
    
    // Update dashboard with user data
    document.getElementById('personal-link').innerHTML = 
        `<a href="${userData.personal_link}" target="_blank">${userData.personal_link}</a>`;
    
    document.getElementById('business-card-link').innerHTML = userData.personal_link;
}

// Animation on scroll
const observerOptions = {
    threshold: 0.1,
    rootMargin: '0px 0px -50px 0px'
};

const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
        if (entry.isIntersecting) {
            entry.target.style.opacity = '1';
            entry.target.style.transform = 'translateY(0)';
        }
    });
}, observerOptions);

// Observe elements for animation
document.querySelectorAll('.feature-card, .card, .step').forEach(el => {
    el.style.opacity = '0';
    el.style.transform = 'translateY(20px)';
    el.style.transition = 'opacity 0.6s ease, transform 0.6s ease';
    observer.observe(el);
});
