// scripts.js

document.addEventListener('DOMContentLoaded', () => {

    // --- 1. Theme Toggle Logic (Dark/Light Mode) ---
    const themeToggle = document.getElementById('themeToggle');
    const htmlElement = document.documentElement;

    // Load saved theme or set to 'light' as default
    const savedTheme = localStorage.getItem('theme') || 'light';
    htmlElement.setAttribute('data-theme', savedTheme);
    updateThemeIcon(savedTheme);

    function updateThemeIcon(theme) {
        if (themeToggle) {
            // Font Awesome icons
            themeToggle.innerHTML = theme === 'dark' ? '<i class="fas fa-sun"></i>' : '<i class="fas fa-moon"></i>';
        }
    }

    if (themeToggle) {
        themeToggle.addEventListener('click', () => {
            const currentTheme = htmlElement.getAttribute('data-theme');
            const newTheme = currentTheme === 'light' ? 'dark' : 'light';
            
            htmlElement.setAttribute('data-theme', newTheme);
            localStorage.setItem('theme', newTheme);
            updateThemeIcon(newTheme);
        });
    }

    // --- 2. FAQ Accordion Logic ---
    const faqItems = document.querySelectorAll('.faq-item');

    faqItems.forEach(item => {
        const question = item.querySelector('.faq-question');
        question.addEventListener('click', () => {
            const isActive = item.classList.contains('active');

            // Close all open items and reset their icons
            faqItems.forEach(i => {
                i.classList.remove('active');
                const icon = i.querySelector('.faq-question i');
                // Ensure the icon is present before manipulating classes
                if (icon) icon.classList.replace('fa-chevron-up', 'fa-chevron-down');
            });

            // Toggle the clicked item (Open it if it was closed)
            if (!isActive) {
                item.classList.add('active');
                const clickedIcon = item.querySelector('.faq-question i');
                if (clickedIcon) clickedIcon.classList.replace('fa-chevron-down', 'fa-chevron-up');
            }
        });
    });

    // --- 3. Sticky Navbar Shadow on Scroll ---
    const nav = document.getElementById('mainNav');
    const shadowClass = 'shadow-lg'; // Class for the shadow effect

    if (nav) {
        window.addEventListener('scroll', () => {
            // Add shadow if scrolled past 50px, remove otherwise
            if (window.scrollY > 50) {
                nav.classList.add(shadowClass); 
            } else {
                nav.classList.remove(shadowClass);
            }
        });
    }

    // --- 4. Mobile Navigation Toggle ---
    const navToggle = document.querySelector('.nav-toggle');
    const navLinks = document.querySelector('.nav-links');

    if (navToggle && navLinks) {
        navToggle.addEventListener('click', () => {
            navLinks.classList.toggle('open');
            // Toggle the menu icon between bars and close
            const icon = navToggle.querySelector('i');
            if (navLinks.classList.contains('open')) {
                icon.classList.replace('fa-bars', 'fa-times');
            } else {
                icon.classList.replace('fa-times', 'fa-bars');
            }
        });

        // Close mobile menu when a link is clicked (for one-page navigation)
        document.querySelectorAll('.nav-links a').forEach(link => {
            link.addEventListener('click', () => {
                if (navLinks.classList.contains('open')) {
                    navLinks.classList.remove('open');
                    navToggle.querySelector('i').classList.replace('fa-times', 'fa-bars');
                }
            });
        });
    }

});
