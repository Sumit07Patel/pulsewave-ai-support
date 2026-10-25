-- Run once in Supabase: SQL Editor > New query > paste > Run
create extension if not exists pgcrypto;

create table if not exists products (
  sku             text primary key,
  name            text not null,
  warranty_months int  not null default 12
);

create table if not exists customers (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  email      text unique not null,
  phone      text,
  created_at timestamptz default now()
);

create table if not exists purchases (
  serial_number text primary key,                       -- e.g. PW-APX-10001
  order_id      text not null,                          -- e.g. ORD-100234
  sku           text not null references products(sku),
  customer_id   uuid not null references customers(id),
  purchase_date date not null
);

create table if not exists rma_tickets (
  ticket_id     text primary key,                       -- e.g. RMA-2610-A1B2C3
  serial_number text not null references purchases(serial_number),
  issue         text,
  status        text not null default 'open',
  created_at    timestamptz default now()
);

-- Lock the tables: with RLS on and no policies, the public anon key can read nothing.
-- Only your backend (service_role key) can access them.
alter table products    enable row level security;
alter table customers   enable row level security;
alter table purchases   enable row level security;
alter table rma_tickets enable row level security;

-- Safe lookup view: no customer name, email or phone.
create or replace view warranty_lookup with (security_invoker = true) as
select p.serial_number,
       p.order_id,
       pr.name as product_name,
       p.purchase_date,
       (p.purchase_date + (pr.warranty_months || ' months')::interval)::date as warranty_end,
       ((p.purchase_date + (pr.warranty_months || ' months')::interval)::date >= current_date) as warranty_active
from purchases p
join products pr on pr.sku = p.sku;

revoke all on warranty_lookup from anon, authenticated;
