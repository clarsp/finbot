CREATE TABLE state_log (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    data JSONB
);

CREATE TABLE trades (
    id SERIAL PRIMARY KEY,
    asset VARCHAR(10),
    direction VARCHAR(4),
    leverage INT,
    exit_target NUMERIC,
    status VARCHAR(20),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);