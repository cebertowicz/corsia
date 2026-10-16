//! macOS DNS-SD discovery for CORSIA hosts.
//!
//! This deliberately uses the operating system's DNS-SD API rather than raw
//! multicast. Besides sharing mDNSResponder's cache, that is how the shell
//! receives Local Network policy failures from macOS.

use std::fmt;

const SERVICE_TYPE: &[u8] = b"_corsia._tcp\0";
const LOCAL_DOMAIN: &[u8] = b"local.\0";

/// A DNS-SD operation could not complete.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DiscoveryError {
    Unsupported,
    Timeout,
    InvalidInstance,
    NoAddress,
    Service(i32),
}

impl fmt::Display for DiscoveryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unsupported => formatter.write_str("DNS-SD discovery is only supported on macOS"),
            Self::Timeout => formatter.write_str("DNS-SD operation timed out"),
            Self::InvalidInstance => formatter.write_str("DNS-SD instance name is invalid"),
            Self::NoAddress => formatter.write_str("DNS-SD returned no address"),
            Self::Service(code) => write!(formatter, "DNS-SD failed ({code})"),
        }
    }
}

impl std::error::Error for DiscoveryError {}

#[cfg(target_os = "macos")]
mod system {
    use super::{DiscoveryError, LOCAL_DOMAIN, SERVICE_TYPE};
    use std::{
        collections::BTreeSet,
        ffi::{CStr, CString},
        net::{IpAddr, Ipv4Addr, Ipv6Addr},
        os::raw::{c_char, c_void},
        ptr,
        sync::{
            atomic::{AtomicBool, Ordering},
            Arc, Mutex,
        },
        thread::{self, JoinHandle},
        time::{Duration, Instant},
    };

    type DNSServiceRef = *mut c_void;
    type DNSServiceFlags = u32;
    type DNSServiceErrorType = i32;

    const DNS_SERVICE_NO_ERROR: DNSServiceErrorType = 0;
    const DNS_SERVICE_FLAGS_ADD: DNSServiceFlags = 0x2;
    const DNS_SERVICE_PROTOCOL_IP_V4: u32 = 0x1;
    const DNS_SERVICE_PROTOCOL_IP_V6: u32 = 0x2;

    type BrowseCallback = unsafe extern "C" fn(
        DNSServiceRef,
        DNSServiceFlags,
        u32,
        DNSServiceErrorType,
        *const c_char,
        *const c_char,
        *const c_char,
        *mut c_void,
    );
    type ResolveCallback = unsafe extern "C" fn(
        DNSServiceRef,
        DNSServiceFlags,
        u32,
        DNSServiceErrorType,
        *const c_char,
        *const c_char,
        u16,
        u16,
        *const u8,
        *mut c_void,
    );
    type AddressCallback = unsafe extern "C" fn(
        DNSServiceRef,
        DNSServiceFlags,
        u32,
        DNSServiceErrorType,
        *const c_char,
        *const libc::sockaddr,
        u32,
        *mut c_void,
    );

    // DNS-SD symbols are exported by Darwin's libSystem; there is no
    // separately linkable libdns_sd on current macOS SDKs.
    #[link(name = "System")]
    extern "C" {
        fn DNSServiceBrowse(
            service_ref: *mut DNSServiceRef,
            flags: DNSServiceFlags,
            interface_index: u32,
            regtype: *const c_char,
            domain: *const c_char,
            callback: Option<BrowseCallback>,
            context: *mut c_void,
        ) -> DNSServiceErrorType;
        fn DNSServiceResolve(
            service_ref: *mut DNSServiceRef,
            flags: DNSServiceFlags,
            interface_index: u32,
            name: *const c_char,
            regtype: *const c_char,
            domain: *const c_char,
            callback: Option<ResolveCallback>,
            context: *mut c_void,
        ) -> DNSServiceErrorType;
        fn DNSServiceGetAddrInfo(
            service_ref: *mut DNSServiceRef,
            flags: DNSServiceFlags,
            interface_index: u32,
            protocol: u32,
            hostname: *const c_char,
            callback: Option<AddressCallback>,
            context: *mut c_void,
        ) -> DNSServiceErrorType;
        fn DNSServiceRefSockFD(service_ref: DNSServiceRef) -> libc::c_int;
        fn DNSServiceProcessResult(service_ref: DNSServiceRef) -> DNSServiceErrorType;
        fn DNSServiceRefDeallocate(service_ref: DNSServiceRef);
    }

    struct BrowseContext {
        names: Arc<Mutex<BTreeSet<String>>>,
    }

    /// Owns a live system DNS-SD browse operation. Dropping it stops the
    /// operation and deallocates the DNSServiceRef.
    pub struct Browser {
        names: Arc<Mutex<BTreeSet<String>>>,
        stop: Arc<AtomicBool>,
        worker: Option<JoinHandle<()>>,
    }

    impl Browser {
        pub fn start() -> Result<Self, DiscoveryError> {
            let names = Arc::new(Mutex::new(BTreeSet::new()));
            let context = Box::into_raw(Box::new(BrowseContext {
                names: Arc::clone(&names),
            }));
            let mut service_ref = ptr::null_mut();
            let error = unsafe {
                DNSServiceBrowse(
                    &mut service_ref,
                    0,
                    0,
                    SERVICE_TYPE.as_ptr().cast(),
                    ptr::null(),
                    Some(browse_callback),
                    context.cast(),
                )
            };
            if error != DNS_SERVICE_NO_ERROR {
                unsafe { drop(Box::from_raw(context)) };
                return Err(DiscoveryError::Service(error));
            }

            let socket = unsafe { DNSServiceRefSockFD(service_ref) };
            if socket < 0 {
                unsafe {
                    DNSServiceRefDeallocate(service_ref);
                    drop(Box::from_raw(context));
                }
                return Err(DiscoveryError::Service(socket));
            }

            let stop = Arc::new(AtomicBool::new(false));
            let worker_stop = Arc::clone(&stop);
            let service_ref = service_ref as usize;
            let context = context as usize;
            let worker = thread::spawn(move || browse_events(service_ref, socket, context, worker_stop));
            Ok(Self {
                names,
                stop,
                worker: Some(worker),
            })
        }

        pub fn names(&self) -> Vec<String> {
            self.names
                .lock()
                .expect("DNS-SD names mutex is not poisoned")
                .iter()
                .cloned()
                .collect()
        }
    }

    impl Drop for Browser {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::Release);
            if let Some(worker) = self.worker.take() {
                let _ = worker.join();
            }
        }
    }

    unsafe extern "C" fn browse_callback(
        _service_ref: DNSServiceRef,
        flags: DNSServiceFlags,
        _interface_index: u32,
        error: DNSServiceErrorType,
        name: *const c_char,
        _regtype: *const c_char,
        _domain: *const c_char,
        context: *mut c_void,
    ) {
        if error != DNS_SERVICE_NO_ERROR || name.is_null() || context.is_null() {
            return;
        }
        let Ok(name) = CStr::from_ptr(name).to_str() else {
            return;
        };
        let context = &*(context as *const BrowseContext);
        apply_browse_event(&context.names, flags & DNS_SERVICE_FLAGS_ADD != 0, name);
    }

    fn browse_events(
        service_ref: usize,
        socket: libc::c_int,
        context: usize,
        stop: Arc<AtomicBool>,
    ) {
        let service_ref = service_ref as DNSServiceRef;
        let context = context as *mut BrowseContext;
        while !stop.load(Ordering::Acquire) {
            let mut poll_fd = libc::pollfd {
                fd: socket,
                events: libc::POLLIN,
                revents: 0,
            };
            let ready = unsafe { libc::poll(&mut poll_fd, 1, 100) };
            if ready > 0 && unsafe { DNSServiceProcessResult(service_ref) } != DNS_SERVICE_NO_ERROR {
                break;
            }
        }
        unsafe {
            DNSServiceRefDeallocate(service_ref);
            drop(Box::from_raw(context));
        }
    }

    /// Resolve a discovered service through both DNSServiceResolve and
    /// DNSServiceGetAddrInfo. The latter is necessary because saved DNS-SD
    /// locators must survive DHCP address changes.
    pub async fn resolve(instance: &str, timeout: Duration) -> Result<(String, u16), DiscoveryError> {
        resolve_blocking(instance, timeout)
    }

    fn resolve_blocking(instance: &str, timeout: Duration) -> Result<(String, u16), DiscoveryError> {
        let instance = CString::new(instance).map_err(|_| DiscoveryError::InvalidInstance)?;
        let deadline = Instant::now() + timeout;
        let (hostname, port) = resolve_service(&instance, deadline)?;
        let address = resolve_address(&hostname, deadline)?;
        Ok((address.to_string(), port))
    }

    struct ResolveContext {
        result: Mutex<Option<Result<(String, u16), DiscoveryError>>>,
    }

    unsafe extern "C" fn resolve_callback(
        _service_ref: DNSServiceRef,
        _flags: DNSServiceFlags,
        _interface_index: u32,
        error: DNSServiceErrorType,
        _fullname: *const c_char,
        hosttarget: *const c_char,
        port: u16,
        _txt_len: u16,
        _txt_record: *const u8,
        context: *mut c_void,
    ) {
        if context.is_null() {
            return;
        }
        let result = if error != DNS_SERVICE_NO_ERROR {
            Err(DiscoveryError::Service(error))
        } else if hosttarget.is_null() {
            Err(DiscoveryError::NoAddress)
        } else {
            CStr::from_ptr(hosttarget)
                .to_str()
                .map(|host| (host.to_owned(), u16::from_be(port)))
                .map_err(|_| DiscoveryError::NoAddress)
        };
        let context = &*(context as *const ResolveContext);
        *context.result.lock().expect("DNS-SD resolve mutex is not poisoned") = Some(result);
    }

    fn resolve_service(instance: &CString, deadline: Instant) -> Result<(String, u16), DiscoveryError> {
        let context = Box::into_raw(Box::new(ResolveContext {
            result: Mutex::new(None),
        }));
        let mut service_ref = ptr::null_mut();
        let error = unsafe {
            DNSServiceResolve(
                &mut service_ref,
                0,
                0,
                instance.as_ptr(),
                SERVICE_TYPE.as_ptr().cast(),
                LOCAL_DOMAIN.as_ptr().cast(),
                Some(resolve_callback),
                context.cast(),
            )
        };
        if error != DNS_SERVICE_NO_ERROR {
            unsafe { drop(Box::from_raw(context)) };
            return Err(DiscoveryError::Service(error));
        }
        wait_for_result(service_ref, context, deadline, |context| {
            context
                .result
                .lock()
                .expect("DNS-SD resolve mutex is not poisoned")
                .take()
        })
    }

    struct AddressContext {
        result: Mutex<Option<Result<IpAddr, DiscoveryError>>>,
    }

    unsafe extern "C" fn address_callback(
        _service_ref: DNSServiceRef,
        _flags: DNSServiceFlags,
        _interface_index: u32,
        error: DNSServiceErrorType,
        _hostname: *const c_char,
        address: *const libc::sockaddr,
        _ttl: u32,
        context: *mut c_void,
    ) {
        if context.is_null() {
            return;
        }
        let result = if error != DNS_SERVICE_NO_ERROR {
            Err(DiscoveryError::Service(error))
        } else {
            ip_from_sockaddr(address).ok_or(DiscoveryError::NoAddress)
        };
        let context = &*(context as *const AddressContext);
        *context.result.lock().expect("DNS-SD address mutex is not poisoned") = Some(result);
    }

    fn resolve_address(hostname: &str, deadline: Instant) -> Result<IpAddr, DiscoveryError> {
        let hostname = CString::new(hostname).map_err(|_| DiscoveryError::NoAddress)?;
        let context = Box::into_raw(Box::new(AddressContext {
            result: Mutex::new(None),
        }));
        let mut service_ref = ptr::null_mut();
        let error = unsafe {
            DNSServiceGetAddrInfo(
                &mut service_ref,
                0,
                0,
                DNS_SERVICE_PROTOCOL_IP_V4 | DNS_SERVICE_PROTOCOL_IP_V6,
                hostname.as_ptr(),
                Some(address_callback),
                context.cast(),
            )
        };
        if error != DNS_SERVICE_NO_ERROR {
            unsafe { drop(Box::from_raw(context)) };
            return Err(DiscoveryError::Service(error));
        }
        wait_for_result(service_ref, context, deadline, |context| {
            context
                .result
                .lock()
                .expect("DNS-SD address mutex is not poisoned")
                .take()
        })
    }

    fn wait_for_result<T, R>(
        service_ref: DNSServiceRef,
        context: *mut T,
        deadline: Instant,
        mut result: impl FnMut(&T) -> Option<Result<R, DiscoveryError>>,
    ) -> Result<R, DiscoveryError> {
        let socket = unsafe { DNSServiceRefSockFD(service_ref) };
        let outcome = if socket < 0 {
            Err(DiscoveryError::Service(socket))
        } else {
            loop {
                if let Some(outcome) = result(unsafe { &*context }) {
                    break outcome;
                }
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    break Err(DiscoveryError::Timeout);
                }
                let mut poll_fd = libc::pollfd {
                    fd: socket,
                    events: libc::POLLIN,
                    revents: 0,
                };
                let timeout_millis = remaining.as_millis().min(i32::MAX as u128) as i32;
                let ready = unsafe { libc::poll(&mut poll_fd, 1, timeout_millis) };
                if ready == 0 {
                    break Err(DiscoveryError::Timeout);
                }
                if ready < 0 {
                    break Err(DiscoveryError::Service(-1));
                }
                let error = unsafe { DNSServiceProcessResult(service_ref) };
                if error != DNS_SERVICE_NO_ERROR {
                    break Err(DiscoveryError::Service(error));
                }
            }
        };
        unsafe {
            DNSServiceRefDeallocate(service_ref);
            drop(Box::from_raw(context));
        }
        outcome
    }

    fn apply_browse_event(names: &Arc<Mutex<BTreeSet<String>>>, added: bool, name: &str) {
        let mut names = names.lock().expect("DNS-SD names mutex is not poisoned");
        if added {
            names.insert(name.to_owned());
        } else {
            names.remove(name);
        }
    }

    fn ip_from_sockaddr(address: *const libc::sockaddr) -> Option<IpAddr> {
        if address.is_null() {
            return None;
        }
        unsafe {
            match (*address).sa_family as libc::c_int {
                libc::AF_INET => {
                    let address = &*(address as *const libc::sockaddr_in);
                    Some(IpAddr::V4(Ipv4Addr::from(address.sin_addr.s_addr.to_ne_bytes())))
                }
                libc::AF_INET6 => {
                    let address = &*(address as *const libc::sockaddr_in6);
                    let bytes = std::slice::from_raw_parts(
                        (&address.sin6_addr as *const libc::in6_addr).cast::<u8>(),
                        16,
                    );
                    let mut octets = [0; 16];
                    octets.copy_from_slice(bytes);
                    Some(IpAddr::V6(Ipv6Addr::from(octets)))
                }
                _ => None,
            }
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn browser_names_are_distinct_sorted_and_remove_fake_dns_sd_events() {
            let names = Arc::new(Mutex::new(BTreeSet::new()));
            apply_browse_event(&names, true, "Studio");
            apply_browse_event(&names, true, "Studio");
            apply_browse_event(&names, true, "Archive");
            apply_browse_event(&names, false, "Studio");

            assert_eq!(
                names.lock().unwrap().iter().cloned().collect::<Vec<_>>(),
                vec!["Archive"]
            );
        }

        #[test]
        fn address_conversion_accepts_fake_ipv4_sockaddr() {
            let mut address: libc::sockaddr_in = unsafe { std::mem::zeroed() };
            address.sin_family = libc::AF_INET as _;
            address.sin_addr.s_addr = u32::from_ne_bytes([192, 168, 1, 20]);

            assert_eq!(
                ip_from_sockaddr((&address as *const libc::sockaddr_in).cast()),
                Some(IpAddr::V4(Ipv4Addr::new(192, 168, 1, 20)))
            );
        }

        #[test]
        #[ignore = "requires a disposable PR 1 host announcing _corsia._tcp; run CORSIA_MDNS_INSTANCE='Studio' cargo test discovery::system::tests::resolve_real_mdns_from_disposable_host -- --ignored --exact"]
        fn resolve_real_mdns_from_disposable_host() {
            let instance = std::env::var("CORSIA_MDNS_INSTANCE")
                .expect("set CORSIA_MDNS_INSTANCE to the disposable host announcement");
            let deadline = Instant::now() + Duration::from_secs(5);
            let instance = CString::new(instance).unwrap();
            let (hostname, port) = resolve_service(&instance, deadline).unwrap();
            let host = resolve_address(&hostname, deadline).unwrap();
            assert!(!host.is_unspecified());
            assert_ne!(port, 0);
        }
    }
}

#[cfg(target_os = "macos")]
#[allow(unused_imports)]
pub use system::{resolve, Browser};

#[cfg(not(target_os = "macos"))]
pub struct Browser;

#[cfg(not(target_os = "macos"))]
impl Browser {
    pub fn start() -> Result<Self, DiscoveryError> {
        Err(DiscoveryError::Unsupported)
    }

    pub fn names(&self) -> Vec<String> {
        Vec::new()
    }
}

#[cfg(not(target_os = "macos"))]
pub async fn resolve(
    _instance: &str,
    _timeout: std::time::Duration,
) -> Result<(String, u16), DiscoveryError> {
    Err(DiscoveryError::Unsupported)
}
