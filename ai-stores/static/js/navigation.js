// Smooth Scroll and Responsive Navigation for AI Stores
(function() {
    'use strict';

    // Enable smooth scrolling for anchor links
    document.addEventListener('DOMContentLoaded', function() {
        // Add smooth scroll behavior to all anchor links
        const anchorLinks = document.querySelectorAll('a[href^="#"]');
        anchorLinks.forEach(link => {
            link.addEventListener('click', function(e) {
                const href = this.getAttribute('href');
                if (href !== '#' && href.length > 1) {
                    const targetId = href.substring(1);
                    const targetElement = document.getElementById(targetId);
                    
                    if (targetElement) {
                        e.preventDefault();
                        targetElement.scrollIntoView({
                            behavior: 'smooth',
                            block: 'start'
                        });
                        
                        // Update URL without triggering scroll
                        if (history.pushState) {
                            history.pushState(null, null, href);
                        }
                    }
                }
            });
        });

        // Initialize responsive mobile menu
        initResponsiveMenu();
    });

    // Responsive Menu Functionality
    function initResponsiveMenu() {
        const menuToggle = document.getElementById('mobile-menu-toggle');
        const mobileMenu = document.getElementById('mobile-menu');
        const navLinks = document.querySelectorAll('.nav-link, #mobile-menu a');
        
        if (!menuToggle || !mobileMenu) {
            return; // Menu elements not found, skip
        }

        // Toggle mobile menu
        menuToggle.addEventListener('click', function() {
            const isOpen = mobileMenu.classList.contains('active');
            
            if (isOpen) {
                closeMobileMenu();
            } else {
                openMobileMenu();
            }
        });

        // Close menu when clicking outside
        document.addEventListener('click', function(event) {
            const isClickInsideMenu = mobileMenu.contains(event.target);
            const isClickOnToggle = menuToggle.contains(event.target);
            
            if (!isClickInsideMenu && !isClickOnToggle && mobileMenu.classList.contains('active')) {
                closeMobileMenu();
            }
        });

        // Close menu when clicking on a link
        navLinks.forEach(link => {
            link.addEventListener('click', function() {
                closeMobileMenu();
            });
        });

        // Close menu on window resize (if resizing to desktop)
        let resizeTimer;
        window.addEventListener('resize', function() {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(function() {
                if (window.innerWidth >= 768 && mobileMenu.classList.contains('active')) {
                    closeMobileMenu();
                }
            }, 250);
        });

        function openMobileMenu() {
            mobileMenu.classList.add('active');
            menuToggle.classList.add('active');
            document.body.style.overflow = 'hidden';
            
            // Animate menu items
            const menuItems = mobileMenu.querySelectorAll('a');
            menuItems.forEach((item, index) => {
                item.style.opacity = '0';
                item.style.transform = 'translateY(-10px)';
                setTimeout(() => {
                    item.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
                    item.style.opacity = '1';
                    item.style.transform = 'translateY(0)';
                }, index * 50);
            });
        }

        function closeMobileMenu() {
            mobileMenu.classList.remove('active');
            menuToggle.classList.remove('active');
            document.body.style.overflow = '';
            
            // Reset menu items
            const menuItems = mobileMenu.querySelectorAll('a');
            menuItems.forEach(item => {
                item.style.opacity = '';
                item.style.transform = '';
                item.style.transition = '';
            });
        }
    }

    // Add smooth scroll behavior to the html element via CSS
    if (document.documentElement) {
        document.documentElement.style.scrollBehavior = 'smooth';
    }
})();

