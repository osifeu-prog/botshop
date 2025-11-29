<?php
header('Content-Type: application/json');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: POST, GET, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');

function getPDO() {
    $database_url = getenv('DATABASE_URL') ?: 'postgresql://postgres:password@localhost:5432/slh_net';

    try {
        if (getenv('DATABASE_URL')) {
            // Railway environment
            $db_params = parse_url(getenv('DATABASE_URL'));
            $dsn = "pgsql:host={$db_params['host']};port={$db_params['port']};dbname=" . ltrim($db_params['path'], '/');
            $username = $db_params['user'];
            $password = $db_params['pass'];
        } else {
            // Local development
            $dsn = "pgsql:host=localhost;port=5432;dbname=slh_net";
            $username = 'postgres';
            $password = 'password';
        }

        $pdo = new PDO($dsn, $username, $password);
        $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
        return $pdo;
    } catch (PDOException $e) {
        error_log("Database connection failed: " . $e->getMessage());
        throw $e;
    }
}

// Initialize additional tables if needed
function initAdditionalTables() {
    try {
        $pdo = getPDO();

        // Check if our tables exist, if not create them
        $pdo->exec('
            CREATE TABLE IF NOT EXISTS website_payments (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                telegram_username VARCHAR(255),
                first_name VARCHAR(255) NOT NULL,
                last_name VARCHAR(255),
                payment_method VARCHAR(50) NOT NULL,
                proof_image VARCHAR(500),
                personal_link VARCHAR(500) NOT NULL,
                status VARCHAR(20) DEFAULT "pending",
                bank_account TEXT,
                group_link TEXT,
                custom_price INTEGER DEFAULT 39,
                bsc_wallet VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ');

        $pdo->exec('
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY,
                bank_account TEXT,
                group_link TEXT,
                custom_price INTEGER DEFAULT 39,
                bsc_wallet VARCHAR(255),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ');

        $pdo->exec('
            CREATE TABLE IF NOT EXISTS site_metrics (
                id SERIAL PRIMARY KEY,
                date DATE UNIQUE,
                visits INTEGER DEFAULT 0,
                unique_visitors INTEGER DEFAULT 0,
                conversions INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ');

    } catch (Exception $e) {
        error_log("Table initialization error: " . $e->getMessage());
    }
}

// Call initialization
initAdditionalTables();
?>
