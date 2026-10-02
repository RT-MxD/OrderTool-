# 🏭 Running Order — Daily Production Entry & Planner

A full-stack production scheduling system for textile/manufacturing units. Operators enter daily machine orders, the system processes them through an AI workflow (LangGraph), and generates printable machine-wise production schedules.

![Architecture](Architecture.png)

---

## ✨ Features

- **📝 Production Data Entry** — Add orders with machine number, design, beam, rate, piece count, and up to 8 feeders
- **⚙️ LangGraph Workflow** — Automatically processes entries (divides piece by 3, groups & sorts by machine)
- **🔍 Smart Filtering** — Query orders by date, machine number, or both
- **🖨️ Printable Schedule** — Generates a clean, print-ready production schedule
- **🐘 Neon PostgreSQL** — Serverless Postgres database (free tier)
- **🚀 Vercel Deployment** — Deploy the API as a serverless function
- **📊 Streamlit UI** — Interactive frontend for data entry and visualization

---

## 📁 Project Structure

```
Running Order/
├── main.py              # FastAPI application (REST API)
├── database.py          # Neon PostgreSQL connection & ORM models
├── schemas.py           # Pydantic request/response schemas
├── workflow.py          # LangGraph workflow (data processing logic)
├── ui.py                # Streamlit frontend
├── requirements.txt     # Python dependencies
├── vercel.json          # Vercel deployment configuration
├── .env.example         # Environment variables template
└── Architecture.png     # System architecture diagram
```

---

## 🛠️ Tech Stack

| Layer        | Technology                                                                 |
|-------------|----------------------------------------------------------------------------|
| **API**      | [FastAPI](https://fastapi.tiangolo.com/) — async Python web framework      |
| **Database** | [Neon PostgreSQL](https://neon.tech/) — serverless Postgres (free tier)     |
| **ORM**      | [SQLAlchemy 2.0](https://www.sqlalchemy.org/) async + `asyncpg` driver     |
| **Workflow** | [LangGraph](https://github.com/langchain-ai/langgraph) — stateful AI graph |
| **Frontend** | [Streamlit](https://streamlit.io/) — interactive data app                  |
| **Hosting**  | [Vercel](https://vercel.com/) — serverless deployment                      |

---

## 🚀 Getting Started

### Prerequisites

- Python 3.10+
- A free [Neon](https://console.neon.tech) account
- (Optional) [Vercel CLI](https://vercel.com/docs/cli) for deployment

### 1. Clone the Repository

```bash
git clone https://github.com/your-username/running-order.git
cd running-order
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Set Up Neon Database

1. Go to [console.neon.tech](https://console.neon.tech) and sign up (free)
2. Create a new project
3. Navigate to **Connection Details** → select driver: **`asyncpg`**
4. Copy the connection string

### 4. Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env` and paste your Neon connection string:

```env
DATABASE_URL=postgresql+asyncpg://user:password@ep-xxxxx.us-east-2.aws.neon.tech/neondb?sslmode=require
```

### 5. Run the API

```bash
uvicorn main:app --reload --port 8000
```

The API will be live at **http://localhost:8000**  
Interactive docs (Swagger UI) at **http://localhost:8000/docs**

### 6. Run the Streamlit Frontend

```bash
streamlit run ui.py
```

---

## 📡 API Endpoints

### Health

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/`      | Health check |

### Orders (CRUD)

| Method   | Endpoint         | Description                           |
|----------|------------------|---------------------------------------|
| `POST`   | `/orders`        | Create a new production order         |
| `GET`    | `/orders`        | List all orders (with optional filters) |
| `GET`    | `/orders/{id}`   | Get a single order by ID              |
| `PATCH`  | `/orders/{id}`   | Partially update an order             |
| `DELETE` | `/orders/{id}`   | Delete an order                       |
| `DELETE` | `/orders`        | Delete all orders                     |

### Workflow

| Method | Endpoint                   | Description                              |
|--------|----------------------------|------------------------------------------|
| `GET`  | `/orders/process/schedule` | Process orders through LangGraph workflow |

### Query Parameters

Both `GET /orders` and `GET /orders/process/schedule` support:

| Parameter    | Type   | Example        | Description              |
|-------------|--------|----------------|--------------------------|
| `order_date` | date   | `2026-10-02`   | Filter by order date     |
| `mc_no`      | string | `M-01`         | Filter by machine number |

### Example: Create an Order

```bash
curl -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{
    "order_date": "2026-10-02",
    "mc_no": "M-01",
    "design_no": "D-1234",
    "beam": "B-100",
    "rate": 150,
    "piece": 300,
    "feeders": {
      "FEEDER 1": "Red",
      "FEEDER 2": "Blue",
      "FEEDER 3": "Green"
    }
  }'
```

### Example: Get Processed Schedule

```bash
curl "http://localhost:8000/orders/process/schedule?order_date=2026-10-02"
```

---

## ☁️ Deploy to Vercel

### 1. Install Vercel CLI

```bash
npm i -g vercel
```

### 2. Deploy

```bash
vercel
```

### 3. Add Environment Variable

```bash
vercel env add DATABASE_URL
# Paste your Neon connection string when prompted
```

### 4. Deploy to Production

```bash
vercel --prod
```

> **⚠️ Important:** You must add `DATABASE_URL` as an environment variable in your Vercel project settings (Settings → Environment Variables) before the first production deploy.

---

## 🗄️ Database Schema

The `production_orders` table:

| Column       | Type         | Description                     |
|--------------|--------------|---------------------------------|
| `id`         | Integer (PK) | Auto-incrementing primary key   |
| `order_date` | Date         | Order date (indexed)            |
| `mc_no`      | String(50)   | Machine number (indexed)        |
| `design_no`  | String(100)  | Design number                   |
| `beam`       | String(100)  | Beam identifier                 |
| `rate`       | Float        | Rate per unit                   |
| `piece`      | Float        | Total piece count               |
| `feeder_1`–`feeder_8` | String(200) | Feeder values (up to 8) |
| `created_at` | DateTime     | Record creation timestamp       |
| `updated_at` | DateTime     | Last update timestamp           |

> Tables are auto-created on first API startup — no manual migration needed.

---

## 🔧 Environment Variables

| Variable       | Required | Description                          |
|---------------|----------|--------------------------------------|
| `DATABASE_URL` | ✅       | Neon PostgreSQL connection string    |

---

## 📄 License

This project is open source and available under the [MIT License](LICENSE).
