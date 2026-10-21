//! Real system-trusted TLS termination, forwarding plain HTTP to the paired host.
use super::*;

async fn plain_host(prover: Prover) -> TestHost {
    let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (forwarded, proofs) = (Arc::new(AtomicUsize::new(0)), Arc::new(AtomicUsize::new(0)));
    let (seen_forwarded, seen_proofs) = (Arc::clone(&forwarded), Arc::clone(&proofs));
    let task = tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let (prover, forwarded, proofs) = (prover.clone(), Arc::clone(&seen_forwarded), Arc::clone(&seen_proofs));
            tokio::spawn(async move {
                let service = service_fn(move |request: Request<Incoming>| {
                    let (prover, forwarded, proofs) = (prover.clone(), Arc::clone(&forwarded), Arc::clone(&proofs));
                    async move {
                        let is_proof = request.method() == Method::POST && request.uri().path() == IDENTITY_PATH;
                        let body = request.into_body().collect().await.unwrap().to_bytes();
                        let text = if is_proof {
                            proofs.fetch_add(1, SeqCst);
                            prover.answer(&body)
                        } else {
                            forwarded.fetch_add(1, SeqCst);
                            "paired host".to_owned()
                        };
                        Ok::<_, Infallible>(Response::new(Full::new(Bytes::from(text))))
                    }
                });
                let _ = hyper::server::conn::http1::Builder::new().serve_connection(TokioIo::new(stream), service).await;
            });
        }
    });
    TestHost { port, forwarded, proofs, task }
}

async fn terminating_proxy(config: Arc<rustls::ServerConfig>, host_port: u16) -> TestHost {
    let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let task = tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let config = Arc::clone(&config);
            tokio::spawn(async move {
                let mut tls = tokio_rustls::TlsAcceptor::from(config).accept(stream).await.unwrap();
                let mut host = TcpStream::connect(("127.0.0.1", host_port)).await.unwrap();
                let _ = tokio::io::copy_bidirectional(&mut tls, &mut host).await;
            });
        }
    });
    TestHost { port, forwarded: Arc::new(AtomicUsize::new(0)), proofs: Arc::new(AtomicUsize::new(0)), task }
}

#[tokio::test]
async fn system_trust_proves_and_forwards_through_a_distinct_tls_terminator() {
    let (host, forwarder) = (test_tls::generated_p256_host(), test_tls::generated_ca_forwarder());
    let backend = plain_host(Prover::honest(&host)).await;
    let outside = terminating_proxy(forwarder.config, backend.port).await;
    let refusals = Arc::new(AtomicUsize::new(0));
    let config = paired(&host, Some((outside.port, system())), Some(forwarder.ca), &refusals);
    let proxy = proxy_for(config, Arc::new(AtomicU16::new(dead_port().await))).await;

    assert_eq!(get(&proxy).await, (StatusCode::OK, "paired host".into()));
    assert_eq!(backend.proofs.load(SeqCst), 1);
    assert_eq!(backend.forwarded.load(SeqCst), 1);
    assert!(!proxy.key_changed());
    assert_eq!(refusals.load(SeqCst), 0);
}

#[tokio::test]
async fn legacy_system_trust_learns_identity_through_a_tls_terminator() {
    let (host, forwarder) = (test_tls::generated_p256_host(), test_tls::generated_ca_forwarder());
    let backend = plain_host(Prover::honest(&host)).await;
    let local = terminating_proxy(forwarder.config, backend.port).await;
    let port = local.port;
    let dial: LocalDial = Arc::new(move || Box::pin(async move { super::super::dial("127.0.0.1", port).await }));
    let config = RouteConfig { tls: client_tls(Some(forwarder.ca)), ..RouteConfig::single(Trust::System, || {}) };
    let routes = Routes::new(config, dial).unwrap();

    assert_eq!(routes.learn_local_identity().await.unwrap(), host.tls.pin);
    assert_eq!(backend.proofs.load(SeqCst), 1);
    assert_eq!(backend.forwarded.load(SeqCst), 0);
}
