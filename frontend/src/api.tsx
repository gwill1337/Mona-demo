// import axios, { type AxiosRequestConfig, AxiosError } from "axios";
// import { API, API_VERSION } from "./config/config";

// export class ApiError extends Error {
//     public status?: number;

//     constructor(message: string, status?: number) {
//         super(message);
//         this.status = status;
//     }
// }

// function extractErrorMessage(error: AxiosError<any>): string {
//     const data = error.response?.data;

//     if (typeof data?.detail === "string") return data.detail;
//     if (typeof data?.detail?.message === "string") return data.detail.message;
//     if (typeof data?.message === "string") return data.message;

//     if (Array.isArray(data?.detail) && data.detail[0]?.msg) {
//         return data.detail[0].msg;
//     }

//     return error.response
//         ? `HTTP ${error.response.status}`
//         : error.message ?? "Network error";
// }

// export async function apiFetch<T = unknown>(
//     endpoint: string,
//     options: AxiosRequestConfig = {}
// ): Promise<T> {
//     try {
//         const response = await axios(API + API_VERSION + endpoint, {
//             withCredentials: true,
//             headers: {
//                 "Content-Type": "application/json",
//                 ...(options.headers ?? {}),
//             },
//             ...options,
//         });

//         return response.data as T;
//     } catch (err) {
//         const error = err as AxiosError<any>;

//         const isLoginRequest = endpoint.includes("/auth/login");

//         if (error.response?.status === 401 && !isLoginRequest) {
//             window.location.href = "/login";
//             throw new Error("Unauthorized");
//         }

//         // throw new Error(extractErrorMessage(error));

//         throw new ApiError(
//             extractErrorMessage(error), 
//             error.response?.status
//         );
//     }
// }

import axios, { type AxiosRequestConfig, AxiosError } from "axios";
import { API, API_VERSION } from "./config/config";

export class ApiError extends Error {
    public status?: number;

    constructor(message: string, status?: number) {
        super(message);
        this.status = status;
    }
}

function extractErrorMessage(error: AxiosError<any>): string {
    const data = error.response?.data;

    if (typeof data?.detail === "string") return data.detail;
    if (typeof data?.detail?.message === "string") return data.detail.message;
    if (typeof data?.message === "string") return data.message;

    if (Array.isArray(data?.detail) && data.detail[0]?.msg) {
        return data.detail[0].msg;
    }

    return error.response
        ? `HTTP ${error.response.status}`
        : error.message ?? "Network error";
}

async function rawApiFetch<T = unknown>(
    endpoint: string,
    options: AxiosRequestConfig = {}
): Promise<T> {
    try {
        const response = await axios(API + API_VERSION + endpoint, {
            withCredentials: true,
            headers: {
                "Content-Type": "application/json",
                ...(options.headers ?? {}),
            },
            ...options,
        });

        return response.data as T;
    } catch (err) {
        const error = err as AxiosError<any>;

        const isLoginRequest = endpoint.includes("/auth/login");

        if (error.response?.status === 401 && !isLoginRequest) {
            window.location.href = "/login";
            throw new ApiError("Unauthorized", 401);
        }

        throw new ApiError(
            extractErrorMessage(error),
            error.response?.status
        );
    }
}

export async function apiFetch<T = unknown>(
    endpoint: string,
    options: AxiosRequestConfig = {}
): Promise<T> {
    return rawApiFetch<T>(endpoint, options);
}

// ─── Retry wrapper, only for GET requests on initial page load ────────────

function isRetryable(e: unknown): boolean {
    if (!(e instanceof ApiError)) return true; // network error, no response at all
    if (e.status === undefined) return true;   // network error, axios set no status
    // don't retry client errors — retrying won't fix a 401/403/404/422
    return e.status >= 500;
}

async function withRetry<T>(
    fn: () => Promise<T>,
    tries = 6,
    delayMs = 5000
): Promise<T> {
    for (let i = 0; ; i++) {
        try {
            return await fn();
        } catch (e) {
            if (i >= tries - 1 || !isRetryable(e)) throw e;
            await new Promise((r) => setTimeout(r, delayMs));
        }
    }
}

export async function apiFetchWithRetry<T = unknown>(
    endpoint: string,
    options: AxiosRequestConfig = {}
): Promise<T> {
    return withRetry(() => rawApiFetch<T>(endpoint, options));
}